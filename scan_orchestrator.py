# arbitrage_bot/scan_orchestrator.py

"""
掃描協調器：每次 BTC-PERPETUAL ticker 更新時觸發，
協調冷卻期檢查、行使價訂閱、機會掃描、交易鎖、最終執行。

同時最多 8 組並行部位，組間冷卻 60 秒。持有部位期間繼續掃描，
但達到上限或冷卻中時僅發通知不下單。
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from config import Config
from deribit_api import get_available_expiries, get_target_strikes, get_funding_rate
from strategy import check_arbitrage_opportunity
from bot_state import bot_state
from global_state import global_state
from trading_engine import perform_final_check_and_execute
from notifications import send_telegram_notification

# 發現機會通知的冷卻計時（10 分鐘內同一機會不重複推送）
_OPPORTUNITY_NOTIFY_COOLDOWN = 600   # 秒
_last_opportunity_notify_time: float = 0.0

logger = logging.getLogger(__name__)


def _notify_opportunity(best: Dict, block_reason: Optional[str]) -> None:
    """
    發現利潤機會時發送 Telegram 通知（10 分鐘冷卻）。
    若無法進場，在訊息末尾附上原因。
    """
    global _last_opportunity_notify_time
    now = time.time()
    if now - _last_opportunity_notify_time < _OPPORTUNITY_NOTIFY_COOLDOWN:
        return
    _last_opportunity_notify_time = now

    if block_reason is None:
        send_telegram_notification(best)
        return

    reason_label = {
        'position_limit': f'⛔ 已達 {Config.MAX_CONCURRENT_POSITIONS} 組部位上限，僅供參考',
        'cooldown':       f'⏳ 冷卻中（剩餘 {max(0, Config.TRADE_COOLDOWN_SECONDS - (now - global_state.last_trade_time)):.0f}s），僅供參考',
        'paused':         '⏸ 機器人已關機（暫停下單），僅供參考',
    }.get(block_reason, f'⚠️ {block_reason}，僅供參考')

    send_telegram_notification_with_reason(best, reason_label)


def send_telegram_notification_with_reason(opportunity: Dict, block_reason_label: str) -> bool:
    """發送套利機會通知，附上無法進場原因。"""
    from notifications import _send_message, _now_tw
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    call_action = '賣出' if opportunity['callDirection'] == 'sell' else '買入'
    put_action  = '賣出' if opportunity['putDirection']  == 'sell' else '買入'
    perp_action = '買入' if opportunity['perpDirection'] == 'long' else '賣出'
    funding_text = f"{opportunity['fundingDirection']} ${opportunity['fundingCost']:.2f}"

    message = f"""
📈 *Deribit 套利機會發現!* 📈

*策略類型*: {opportunity['strategyName']}
*到期日*: {opportunity['expiryDate']}
*履約價*: ${opportunity['strike']}

--- *下單參數* ---
• *{call_action} Call*: `{opportunity['callInstrument']}` @ `{opportunity['callPrice']:.4f} BTC`
• *{put_action} Put*: `{opportunity['putInstrument']}` @ `{opportunity['putPrice']:.4f} BTC`
• *{perp_action} Perpetual*: `BTC-PERPETUAL` @ `${opportunity['perpOpenPrice']:.2f}`

--- *財務分析* ---
• *預估淨利潤*: `${opportunity['netProfit']:.2f}`
• *理論利潤*: `${opportunity['grossProfit']:.2f}`
• *Call 手續費*: `-${opportunity.get('callFee', 0):.2f}`
• *Put 手續費*: `-${opportunity.get('putFee', 0):.2f}`
• *Perp 手續費*: `-${opportunity.get('perpOpenFee', 0) + opportunity.get('perpCloseFee', 0):.2f}` (進+平倉)
• *手續費合計*: `-${opportunity['totalFees']:.2f}`
• *預估資金費率*: `{funding_text}`
• *所需保證金 (估算)*: `${opportunity['margin']:.0f}`

{block_reason_label}

_資料時間: {timestamp}_
""".strip()

    return _send_message(message)


def run_scan(ws_client, trader, pos_manager, trigger: str = 'fallback') -> None:
    """
    單次掃描邏輯。
    由事件 callback（on_ticker）或兜底 loop 呼叫。
    """
    try:
        # ── 節流 ──────────────────────────────────────────────────────────────
        if not global_state.should_scan():
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
            bot_state.update_funding_rate(funding_rate_8h)
        else:
            funding_rate_8h = get_funding_rate(ws_client)    # fallback

        expiry_list = get_available_expiries()
        if not expiry_list:
            return

        expiry_dates = [e['dateStr'] for e in expiry_list]
        if expiry_dates != getattr(global_state, '_last_expiry_dates', []):
            logger.info(f"✅ 掃描到期日: {', '.join(expiry_dates)}")
            global_state._last_expiry_dates = expiry_dates

        # ── 收集所有到期日的履約價，一次訂閱 ──────────────────────────────
        expiry_strikes_map: Dict = {}  # {dateStr: (expiry_info, strikes)}
        all_instruments_needed: List[str] = []

        for expiry_info in expiry_list:
            strikes = get_target_strikes(perp_ticker['last_price'], expiry_info['dateStr'])
            if strikes:
                expiry_strikes_map[expiry_info['dateStr']] = (expiry_info, strikes)
                for s in strikes:
                    all_instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(s)}-C")
                    all_instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(s)}-P")

        if not expiry_strikes_map:
            return

        instruments_set = set(all_instruments_needed) | {'BTC-PERPETUAL'}
        if instruments_set != global_state.current_instruments:
            ws_client.subscribe_instruments(all_instruments_needed)
            global_state.current_instruments = instruments_set
            if not ws_client.wait_for_data(all_instruments_needed, timeout=10):
                logger.warning('⚠️ 部分數據未就緒，繼續執行')

        # ── 掃描所有到期日 × 履約價 ─────────────────────────────────────────
        all_opportunities: List[Dict] = []
        for date_str, (expiry_info, strikes) in expiry_strikes_map.items():
            for strike in strikes:
                result = check_arbitrage_opportunity(
                    strike, expiry_info, ws_client, perp_ticker, funding_rate_8h
                )
                if result:
                    if result['strategyA'] and result['strategyA']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                        all_opportunities.append(result['strategyA'])
                    if result['strategyB'] and result['strategyB']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                        all_opportunities.append(result['strategyB'])

        # ── 更新持倉狀態顯示 ──────────────────────────────────────────────────
        with pos_manager.lock:
            n_positions = len(pos_manager.active_positions)

        _now = time.time()
        _cooldown_remaining = max(0, Config.TRADE_COOLDOWN_SECONDS - (_now - global_state.last_trade_time)) \
            if global_state.last_trade_time > 0 else 0

        # 保留上一次 ws 觸發掃描的時間
        _prev_ws_time = (bot_state.scan_info or {}).get('last_ws_scan_time')
        _last_ws_scan_time = _now if trigger == 'ws' else _prev_ws_time

        _common_scan_info = {
            'last_scan_time':             _now,
            'last_ws_scan_time':          _last_ws_scan_time,
            'trigger':                    trigger,
            'expiry_date':                expiry_dates[0] if expiry_dates else '',
            'expiry_dates':               expiry_dates,
            'positions_held':             n_positions,
            'position_count':             n_positions,
            'trade_cooldown_remaining_sec': round(_cooldown_remaining),
        }

        if not all_opportunities:
            logger.debug(f'📊 未發現高利潤機會 (> ${Config.MIN_NET_PROFIT_OPPORTUNITY})')
            bot_state.update_scan_info({'status': 'no_opportunity', **_common_scan_info})
            return

        # ── 選出最佳機會 ───────────────────────────────────────────────────────
        best = max(all_opportunities, key=lambda x: x['netProfit'])
        logger.info(
            f"🏆 最佳機會: {best['strategyName']} @ ${best['strike']} "
            f"淨利=${best['netProfit']:.2f}"
        )
        bot_state.update_scan_info({
            'status':        'opportunity_found',
            'strike':         best['strike'],
            'best_profit':    best['netProfit'],
            'strategy_name':  best['strategyName'],
            **_common_scan_info,
        })

        # ── 計算是否可以進場 ──────────────────────────────────────────────────
        can_trade   = True
        block_reason = None

        if n_positions >= Config.MAX_CONCURRENT_POSITIONS:
            can_trade    = False
            block_reason = 'position_limit'
            logger.debug(f"📊 已達 {Config.MAX_CONCURRENT_POSITIONS} 組部位上限，暫停進場")

        if can_trade and not bot_state.trading_enabled:
            can_trade = False
            block_reason = 'paused'

        if can_trade:
            cooldown_elapsed = time.time() - global_state.last_trade_time
            if global_state.last_trade_time > 0 and cooldown_elapsed < Config.TRADE_COOLDOWN_SECONDS:
                remaining = Config.TRADE_COOLDOWN_SECONDS - cooldown_elapsed
                can_trade    = False
                block_reason = 'cooldown'
                logger.debug(f"⏳ 組間冷卻中，剩餘 {remaining:.0f}s")

        # ── 有機會就通知（不論能否進場）── 用 background thread 避免阻塞交易路徑
        threading.Thread(
            target=_notify_opportunity,
            args=(best, block_reason if not can_trade else None),
            daemon=True,
        ).start()

        if not can_trade:
            return

        # ── Fix #1: non-blocking 取得交易鎖 ───────────────────────────────────
        if not global_state._trade_lock.acquire(blocking=False):
            logger.info("⚡️ 其他執行緒正在交易中，跳過本次機會")
            return
        try:
            perform_final_check_and_execute(best, ws_client, trader, pos_manager)
        finally:
            global_state._trade_lock.release()

    except Exception as e:
        logger.error(f'❌ run_scan 發生錯誤: {e}', exc_info=True)
