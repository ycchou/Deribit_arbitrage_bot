# arbitrage_bot/scan_orchestrator.py

"""
掃描協調器：每次 BTC-PERPETUAL ticker 更新時觸發，
協調冷卻期檢查、行使價訂閱、機會掃描、交易鎖、最終執行。

Fix #1  _trade_lock non-blocking acquire，避免重複執行
Fix #8  失敗冷卻期檢查
Fix #9  鎖內雙重確認無活躍部位（硬性上限 = 1）
"""

import logging
import time
from typing import Dict, List

from config import Config
from deribit_api import get_tomorrow_expiry, get_target_strikes
from strategy import check_arbitrage_opportunity
from bot_state import bot_state
from global_state import global_state
from trading_engine import perform_final_check_and_execute

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

        # ── 28 小時冷卻期 ─────────────────────────────────────────────────────
        elapsed = time.time() - global_state.last_trade_time
        if elapsed < Config.COOLDOWN_PERIOD_SECONDS:
            remaining = (Config.COOLDOWN_PERIOD_SECONDS - elapsed) / 60
            logger.info(f"❄️ 冷卻期剩餘 {remaining:.1f} 分鐘")
            bot_state.update_scan_info({
                'status':                'cooling_down',
                'cooldown_remaining_min': round(remaining, 1),
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

        # ── 取得市場數據 ───────────────────────────────────────────────────────
        perp_ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if not perp_ticker or not perp_ticker.get('last_price'):
            return
        bot_state.update_btc_price(perp_ticker['last_price'])
        if 'funding_8h' in perp_ticker:   # Fix #6: 即時更新 funding rate
            bot_state.update_funding_rate(perp_ticker['funding_8h'])

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

        # ── 掃描套利機會 ───────────────────────────────────────────────────────
        all_opportunities: List[Dict] = []
        for strike in strikes:
            result = check_arbitrage_opportunity(strike, expiry_info, ws_client)
            if result:
                if result['strategyA'] and result['strategyA']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyA'])
                if result['strategyB'] and result['strategyB']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyB'])

        if not all_opportunities:
            logger.info(f'📊 未發現高利潤機會 (> ${Config.MIN_NET_PROFIT_OPPORTUNITY})')
            bot_state.update_scan_info({
                'status':         'no_opportunity',
                'last_scan_time': time.time(),
                'expiry_date':    expiry_info['dateStr'],
            })
            return

        # ── 選出最佳機會 ───────────────────────────────────────────────────────
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
