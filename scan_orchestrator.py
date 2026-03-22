# arbitrage_bot/scan_orchestrator.py

"""
掃描協調器：每次 BTC-PERPETUAL ticker 更新時觸發，
協調冷卻期檢查、行使價訂閱、機會掃描、交易鎖、最終執行。

冷卻邏輯：持有倉位期間不掃描，倉位到期平倉後立即恢復。
Fix #1  _trade_lock non-blocking acquire，避免重複執行
Fix #8  下單失敗後 5 分鐘短暫冷卻
Fix #9  鎖內雙重確認無活躍部位（硬性上限 = 1）
"""

import logging
import time
from typing import Dict, List

from config import Config
from deribit_api import get_tomorrow_expiry, get_target_strikes, get_funding_rate
from strategy import check_arbitrage_opportunity
from bot_state import bot_state
from global_state import global_state
from trading_engine import perform_final_check_and_execute
from notifications import send_telegram_notification

# 發現機會通知的冷卻計時（10 分鐘內同一機會不重複推送）
_OPPORTUNITY_NOTIFY_COOLDOWN = 600   # 秒
_last_opportunity_notify_time: float = 0.0

logger = logging.getLogger(__name__)


def run_scan(ws_client, trader, pos_manager) -> None:
    """
    單次掃描邏輯。
    由事件 callback（on_ticker）或兜底 loop 呼叫。
    """
    try:
        # ── 節流 ──────────────────────────────────────────────────────────────
        if not global_state.should_scan():
            return

        # ── 持倉中：等待倉位到期平倉後才恢復掃描 ─────────────────────────────
        with pos_manager.lock:
            has_position = pos_manager.active_position is not None
            pos_expiry   = (pos_manager.active_position or {}).get('expiry_timestamp')

        if has_position:
            remaining_sec = max(0, (pos_expiry / 1000) - time.time()) if pos_expiry else 0
            logger.debug(f"📦 持倉中，等待到期（剩餘 {remaining_sec:.0f}s）")
            bot_state.update_scan_info({
                'status':         'position_open',
                'last_scan_time': time.time(),
            })
            return

        # ── Fix #8: 下單失敗短暫冷卻 ──────────────────────────────────────────
        failure_elapsed = time.time() - global_state.last_failure_time
        if failure_elapsed < Config.FAILURE_COOLDOWN_SECONDS:
            remaining_sec = Config.FAILURE_COOLDOWN_SECONDS - failure_elapsed
            logger.info(f"⚠️ 失敗冷卻中，剩餘 {remaining_sec:.0f}s")
            bot_state.update_scan_info({
                'status':                'cooling_down',
                'cooldown_remaining_min': round(remaining_sec / 60, 2),
            })
            return

        # ── 取得市場數據（一次加鎖，供後續迴圈共用）──────────────────────────
        perp_ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if not perp_ticker or not perp_ticker.get('last_price'):
            return
        bot_state.update_btc_price(perp_ticker['last_price'])

        # 從已取得的 perp_ticker 直接讀取 funding rate，避免重複加鎖
        funding_rate_8h = perp_ticker.get('funding_8h')
        if funding_rate_8h is not None:
            bot_state.update_funding_rate(funding_rate_8h)   # Fix #6
        else:
            funding_rate_8h = get_funding_rate(ws_client)    # fallback

        expiry_info = get_tomorrow_expiry()
        if not expiry_info:
            return

        if expiry_info['dateStr'] != global_state.last_expiry_date:
            logger.info(f"✅ 目標到期日: {expiry_info['dateStr']} ({expiry_info['fullDate']})")
            global_state.last_expiry_date = expiry_info['dateStr']

        # ── 訂閱所需合約 ───────────────────────────────────────────────────────
        strikes = get_target_strikes(perp_ticker['last_price'], expiry_info['dateStr'])
        if not strikes:
            return

        instruments_needed = [
            f"BTC-{expiry_info['dateStr']}-{int(s)}-{side}"
            for s in strikes
            for side in ('C', 'P')
        ]
        instruments_set = set(instruments_needed) | {'BTC-PERPETUAL'}
        if instruments_set != global_state.current_instruments:
            ws_client.subscribe_instruments(instruments_needed)
            global_state.current_instruments = instruments_set
            if not ws_client.wait_for_data(instruments_needed, timeout=10):
                logger.warning('⚠️ 部分數據未就緒，繼續執行')

        # ── 掃描套利機會（傳入預取資料，避免每個 strike 重複加鎖）──────────
        all_opportunities: List[Dict] = []
        for strike in strikes:
            result = check_arbitrage_opportunity(
                strike, expiry_info, ws_client, perp_ticker, funding_rate_8h
            )
            if result:
                if result['strategyA'] and result['strategyA']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyA'])
                if result['strategyB'] and result['strategyB']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyB'])

        if not all_opportunities:
            logger.debug(f'📊 未發現高利潤機會 (> ${Config.MIN_NET_PROFIT_OPPORTUNITY})')
            bot_state.update_scan_info({
                'status':         'no_opportunity',
                'last_scan_time': time.time(),
                'expiry_date':    expiry_info['dateStr'],
            })
            return

        # ── 選出最佳機會 ───────────────────────────────────────────────────────
        global _last_opportunity_notify_time
        best = max(all_opportunities, key=lambda x: x['netProfit'])
        logger.info(
            f"🏆 最佳機會: {best['strategyName']} @ ${best['strike']} "
            f"淨利=${best['netProfit']:.2f}"
        )
        bot_state.update_scan_info({
            'status':         'opportunity_found',
            'strike':         best['strike'],
            'best_profit':    best['netProfit'],
            'strategy_name':  best['strategyName'],
            'last_scan_time': time.time(),
            'expiry_date':    expiry_info['dateStr'],
        })

        # ── 發現機會通知（10 分鐘冷卻，避免同一機會重複推送）──────────────
        now = time.time()
        if now - _last_opportunity_notify_time >= _OPPORTUNITY_NOTIFY_COOLDOWN:
            _last_opportunity_notify_time = now
            send_telegram_notification(best)

        # ── Fix #1: non-blocking 取得交易鎖 ───────────────────────────────────
        if not global_state._trade_lock.acquire(blocking=False):
            logger.info("⚡️ 其他執行緒正在交易中，跳過本次機會")
            return
        try:
            # Fix #9: 鎖內雙重確認無活躍部位
            with pos_manager.lock:
                if pos_manager.active_position:
                    logger.info(
                        f"⚠️ 已有活躍部位（{pos_manager.active_position.get('status')}），"
                        "跳過執行"
                    )
                    return
            perform_final_check_and_execute(best, ws_client, trader, pos_manager)
        finally:
            global_state._trade_lock.release()

    except Exception as e:
        logger.error(f'❌ run_scan 發生錯誤: {e}', exc_info=True)
