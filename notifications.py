# arbitrage_bot/notifications.py

"""
負責發送所有類型的通知，例如 Telegram。
"""
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List

_TZ_TAIPEI = timezone(timedelta(hours=8))

def _now_tw() -> datetime:
    """回傳目前 UTC+8 時間。"""
    return datetime.now(_TZ_TAIPEI)

from config import Config
from profit_calculator import (
    OPTION_TAKER_FEE_RATE, PERP_TAKER_FEE_RATE, PERP_MAKER_FEE_RATE,
    calculate_strategy,
)

logger = logging.getLogger(__name__)

def _send_message(message: str) -> bool:
    """通用訊息發送函式"""
    url = f'{Config.TELEGRAM_BASE_URL}/sendMessage'
    payload = {
        'chat_id': Config.TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        
        if not result.get('ok'):
            logger.error(f"❌ Telegram 發送失敗: {result.get('description')}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f'❌ 發送 Telegram 訊息時發生網路錯誤: {e}')
        return False
    except Exception as e:
        logger.error(f'❌ 發送 Telegram 訊息時發生未知錯誤: {e}')
        return False

def send_verification_code(code: str, action_description: str) -> bool:
    """發送 Dashboard 操作驗證碼到 Telegram。"""
    msg = (
        f"🔐 *Dashboard 操作驗證*\n\n"
        f"驗證碼: `{code}`\n"
        f"操作: {action_description}\n\n"
        f"⏰ 5 分鐘內有效"
    )
    return _send_message(msg)


def send_power_notification(enabled: bool) -> bool:
    """發送開關機確認通知。開機時附上完整下單設定。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    if enabled:
        env = "🧪 Testnet" if Config.USE_TESTNET else "🔴 Mainnet"
        msg = f"""🟢 *機器人已開機，開始下單*

*下單設定*:
• 環境: {env}
• 交易單位: `{Config.TRADE_AMOUNT_BTC} BTC`
• 最低淨利門檻: `${Config.MIN_NET_PROFIT_OPPORTUNITY}`
• Strike 掃描範圍: `±{Config.STRIKE_SCAN_RANGE}`
• 到期日範圍: `{Config.EXPIRY_MIN_HOURS}h ~ {Config.EXPIRY_MAX_HOURS}h`
• 最大並行部位: `{Config.MAX_CONCURRENT_POSITIONS}`
• 交易冷卻: `{Config.TRADE_COOLDOWN_SECONDS}s`

_時間: {timestamp}_""".strip()
    else:
        msg = f"🔴 *機器人已關機*\n\n已停止下單，現有持倉繼續管理至到期，掃描持續運作。\n\n_時間: {timestamp}_"
    return _send_message(msg)


def send_telegram_notification(opportunity: Dict) -> bool:
    """格式化套利機會訊息並透過 Telegram Bot 發送（用於一般監控）"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    
    call_action = '賣出' if opportunity['callDirection'] == 'sell' else '買入'
    put_action = '賣出' if opportunity['putDirection'] == 'sell' else '買入'
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
• *預估資金費率*: `{funding_text}` (基於當前費率 {opportunity['fundingRate24h']:.4f}% 估算24H)
• *所需保證金 (估算)*: `${opportunity['margin']:.0f}`

⚠️ *注意*: 永續合約將在期權到期前自動平倉（Maker 優先，10s 內未成交改市價）。利潤估算已包含平倉費用。

_資料時間: {timestamp}_
_數據來源: WebSocket 實時訂閱_
""".strip()
    
    success = _send_message(message)
    if success:
        logger.info(f"✅ 成功發送 Telegram 通知 (履約價 ${opportunity['strike']})")
    return success

def send_trade_execution_notification(opportunity: Dict) -> bool:
    """發送成功執行交易的通知，含各腿實際成交價、手續費、損益明細。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    # ── 實際成交價 ────────────────────────────────────────────────────────
    perp_fill = opportunity.get('fill_perp_price') or opportunity.get('perpOpenPrice', 0.0)
    call_fill = opportunity.get('fill_call_price') or opportunity.get('callPrice', 0.0)
    put_fill  = opportunity.get('fill_put_price')  or opportunity.get('putPrice',  0.0)
    amount    = Config.TRADE_AMOUNT_BTC

    # ── 從 fill-based calculate_strategy 結果取得各腿手續費 ──────────────
    call_entry_fee = opportunity.get('callFee', 0.0)
    put_entry_fee  = opportunity.get('putFee',  0.0)
    perp_entry_fee = opportunity.get('perpOpenFee', 0.0)
    perp_close_fee = opportunity.get('perpCloseFee', 0.0)
    entry_fees     = call_entry_fee + put_entry_fee + perp_entry_fee

    gross_profit = opportunity.get('grossProfit', 0.0)
    total_fees   = opportunity.get('totalFees',   0.0)
    funding_cost = opportunity.get('fundingCost', 0.0)   # 永遠正值
    funding_dir  = opportunity.get('fundingDirection', '')
    net_profit   = opportunity.get('netProfit',   0.0)
    margin       = opportunity.get('margin',      0.0)
    exit_fee_est = perp_close_fee                         # 估算平倉費

    # ── 換算 ─────────────────────────────────────────────────────────────
    call_usd = call_fill * perp_fill
    put_usd  = put_fill  * perp_fill

    # ── 期權到期時間（UTC+8）────────────────────────────────────────────
    expiry_ts = opportunity.get('expiryTimestamp', 0)
    if expiry_ts:
        expiry_dt  = datetime.fromtimestamp(expiry_ts / 1000, tz=_TZ_TAIPEI)
        expiry_str = expiry_dt.strftime('%Y-%m-%d %H:%M') + ' UTC+8'
    else:
        expiry_str = opportunity.get('expiryDate', 'N/A')

    call_action = '買入' if opportunity['callDirection'] == 'buy' else '賣出'
    put_action  = '買入' if opportunity['putDirection']  == 'buy' else '賣出'
    perp_action = '賣出' if opportunity['perpDirection'] == 'short' else '買入'

    net_icon      = '🟢' if net_profit >= 0 else '🔴'
    funding_sign  = '+' if funding_dir == '收入' else '-'
    funding_label = f"`{funding_sign}${funding_cost:.2f}` ({funding_dir}，估)"

    message = f"""
🚀 *套利交易已成功執行* 🚀

*{opportunity['strategyName']}*  履約價 `${opportunity['strike']}`  數量 `{amount} BTC`

--- *成交明細* ---
• *{call_action} Call* `{opportunity['callInstrument']}`
  成交價: `{call_fill:.4f} BTC` ≈ `${call_usd:.0f}` | 手續費: `-${call_entry_fee:.2f}`
• *{put_action} Put* `{opportunity['putInstrument']}`
  成交價: `{put_fill:.4f} BTC` ≈ `${put_usd:.0f}` | 手續費: `-${put_entry_fee:.2f}`
• *{perp_action} Perp* `BTC-PERPETUAL`
  成交價: `${perp_fill:,.0f}` | 進場費: `-${perp_entry_fee:.2f}`

--- *手續費明細（以成交價計算）* ---
• *Call 手續費*: `-${call_entry_fee:.2f}`
• *Put 手續費*: `-${put_entry_fee:.2f}`
• *Perp 進場費*: `-${perp_entry_fee:.2f}`
• *Perp 平倉費 (估)*: `-${exit_fee_est:.2f}` (Taker 估算)
• *手續費合計*: `-${total_fees:.2f}`

--- *財務摘要* ---
• *毛利*: `${gross_profit:+.2f}`
• *資金費率 (估)*: {funding_label}
• *預估淨利*: {net_icon} `${net_profit:+.2f}`
• *保證金使用 (估算)*: `${margin:.0f}`

*期權到期*: {expiry_str}
*執行時間*: {timestamp}
""".strip()

    logger.info(f"發送交易執行成功通知 (履約價 ${opportunity['strike']})")
    return _send_message(message)

def send_startup_notification() -> bool:
    """發送機器人啟動通知（預設關機狀態，僅監控市場）"""
    env = "🧪 Testnet" if Config.USE_TESTNET else "🔴 Mainnet"
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    message = f"""
🤖 *Deribit 套利機器人已啟動*

• *環境*: {env}
• *啟動時間*: {timestamp}

⏸ *預設為關機狀態，掃描市場但不下單。*
請至 Dashboard 按下開機鍵並通過驗證後開始交易。
""".strip()
    logger.info("發送啟動通知")
    return _send_message(message)


def send_position_closed_notification(position: Dict, close_method: str) -> bool:
    """發送部位平倉通知，含實際平倉價、真實手續費與估算損益。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    entry_time = position.get('entry_time', 0)
    duration_s = int(datetime.utcnow().timestamp() - entry_time) if entry_time else 0
    hours, rem = divmod(duration_s, 3600)
    minutes    = rem // 60
    margin     = position.get('margin_est', 0.0)

    method_label = {
        'maker':   '✅ Maker 限價成交',
        'taker':   '⚡ 到期前市價沖銷',
        'expired': '📅 期權到期結算 / 永續緊急市價平倉',
    }.get(close_method, close_method)

    fill_call  = position.get('fill_call_price', 0.0)
    fill_put   = position.get('fill_put_price', 0.0)
    fill_perp  = position.get('fill_perp_price', 0.0)
    close_perp = position.get('close_perp_price')
    strike     = position.get('strike', 0.0)
    amount     = position.get('amount', Config.TRADE_AMOUNT_BTC)

    if close_perp and fill_perp and fill_call:
        # ── 使用 profit_calculator 做真相來源的重算 ──────────────────────────
        # 與 position_manager / trading_engine / 前端完全同源，避免多套公式
        # 不一致（H1a/H1b/H1d）。關鍵參數：
        #   • perp_close_price = close_perp（實際平倉價）
        #   • perpetual_price  = close_perp（以平倉價估值期權 BTC 成本）
        #   • funding_rate_24h = 實際持倉時長加權
        strategy_type = 'B' if position.get('call_direction') == 'buy' else 'A'
        call_inst     = position.get('call_instrument', '')
        date_str      = call_inst.split('-')[1] if len(call_inst.split('-')) >= 2 else ''
        expiry_ts     = position.get('expiry_timestamp', 0)

        # 依「實際持倉時長」加權資金費率（與 trading_engine.py:103 / position_manager 一致）
        # 使用 bot_state 的當下費率做近似（真實應為時間積分，但未追蹤歷史費率）
        try:
            from bot_state import bot_state as _bs
            funding_rate_8h = float(_bs.funding_rate or 0.0)
        except Exception:
            funding_rate_8h = 0.0
        hold_hours        = max(0, duration_s) / 3600
        funding_rate_hold = funding_rate_8h * max(1, hold_hours / 8)

        expiry_info = {
            'dateStr':   date_str,
            'fullDate':  date_str,
            'timestamp': expiry_ts,
        }

        result = calculate_strategy(
            strategy_type    = strategy_type,
            strategy_name    = position.get('strategy_name', ''),
            call_price       = fill_call,
            put_price        = fill_put,
            perp_open_price  = fill_perp,
            perp_close_price = close_perp,
            strike           = strike,
            perpetual_price  = close_perp,
            funding_rate_24h = funding_rate_hold,
            expiry_info      = expiry_info,
            call_instrument  = call_inst,
            put_instrument   = position.get('put_instrument', ''),
            call_direction   = position.get('call_direction', 'buy'),
            put_direction    = position.get('put_direction', 'sell'),
            perp_direction   = position.get('perp_direction', 'short'),
            amount           = amount,
        )

        # 從 calculate_strategy 取得各腿手續費
        call_fee       = result['callFee']
        put_fee        = result['putFee']
        perp_open_fee  = result['perpOpenFee']
        perp_close_fee_taker = result['perpCloseFee']

        # calculate_strategy 預設 perp 進出場都 taker，maker 平倉時需調整
        perp_close_notional = close_perp * amount
        if close_method == 'maker':
            perp_close_fee_actual = perp_close_notional * PERP_MAKER_FEE_RATE  # 負 = 回扣
            total_fees_adjusted = result['totalFees'] - perp_close_fee_taker + perp_close_fee_actual
        else:
            perp_close_fee_actual = perp_close_fee_taker
            total_fees_adjusted = result['totalFees']

        # 毛利 / funding 直接用 calculate_strategy 的結果
        actual_gross     = result['grossProfit']
        funding_cost_abs = result['fundingCost']
        funding_dir      = result['fundingDirection']
        funding_signed   = funding_cost_abs if funding_dir == '支付' else -funding_cost_abs
        funding_sign_str = '-' if funding_dir == '支付' else '+'

        # 淨損益（對齊 profit_calculator:71 的邏輯）
        actual_net = actual_gross - total_fees_adjusted - funding_signed

        # Perp 單腿損益：依實際方向計算（short: open-close, long: close-open）
        if position.get('perp_direction') == 'long':
            perp_pnl = (close_perp - fill_perp) * amount
        else:
            perp_pnl = (fill_perp - close_perp) * amount

        net_icon  = '🟢' if actual_net >= 0 else '🔴'
        perp_icon = '🟢' if perp_pnl >= 0 else '🔴'

        if close_method == 'maker':
            close_fee_str = f'`+${abs(perp_close_fee_actual):.2f}` (Maker 回扣)'
        else:
            close_fee_str = f'`-${perp_close_fee_actual:.2f}` (Taker 市價)'

        pnl_block = f"""
--- *平倉明細* ---
• Perp: 進場 `${fill_perp:,.0f}` → 平倉 `${close_perp:,.0f}`
• Perp 損益: {perp_icon} `${'%+.2f' % perp_pnl}`
• 期權: 持有至到期，由 Deribit 以 index price 結算

--- *手續費明細（實際）* ---
• *Call 手續費*: `-${call_fee:.2f}`
• *Put 手續費*: `-${put_fee:.2f}`
• *Perp 進場費*: `-${perp_open_fee:.2f}`
• *Perp 平倉費*: {close_fee_str}
• *手續費合計*: `-${total_fees_adjusted:.2f}`

--- *最終損益* ---
• 毛利: `${'%+.2f' % actual_gross}`
• 資金費率: `{funding_sign_str}${funding_cost_abs:.2f}` ({funding_dir})
• *淨損益*: {net_icon} `${'%+.2f' % actual_net}`"""
    else:
        # expired 或資料不足，退回進場時預估值
        net_profit = position.get('net_profit_est', 0.0)
        net_icon   = '🟢' if net_profit >= 0 else '🔴'
        pnl_block  = f"""
*財務摘要（進場估算）*:
  • *預估淨收益*: {net_icon} `${net_profit:.2f}`"""

    message = f"""
📦 *部位已平倉* 📦

*{position.get('strategy_name', '')}* @ `${strike:,.0f}` | `{amount} BTC`
*平倉方式*: {method_label}
*持倉時長*: {hours}h {minutes}m
*平倉時間*: {timestamp}
{pnl_block}
• *保證金使用（估算）*: `${margin:.0f}`

⚠️ _期權損益以平倉時 BTC 價估算，實際以 Deribit 結算為準。_
""".strip()

    logger.info(f"發送平倉通知（{close_method}）")
    return _send_message(message)


def send_liquidity_issue_notification(opportunity: Dict) -> bool:
    """發送因流動性不足而放棄交易的通知"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    
    message = f"""
⚠️ *放棄交易：流動性不足* ⚠️

機器人發現一個高利潤的套利機會，但因市場深度不足以支撐 `{Config.TRADE_AMOUNT_BTC} BTC` 的交易而被放棄。

*機會詳情*:
  • *策略類型*: {opportunity['strategyName']}
  • *履約價*: `${opportunity['strike']}`
  • *預估淨利潤*: `${opportunity['netProfit']:.2f}`

*流動性檢查* (所需單位: {Config.TRADE_AMOUNT_BTC}):
  • *Call 掛單量*: `{opportunity['callAmount']:.2f}`
  • *Put 掛單量*: `{opportunity['putAmount']:.2f}`
  • *永續掛單量*: `{opportunity['perpAmount']:.2f}`

*結論*:
機器人將繼續監控市場，尋找下一個合適的機會。

_偵測時間: {timestamp}_
""".strip()

    logger.info(f"發送流動性不足通知 (履約價 ${opportunity['strike']})")
    return _send_message(message)


# ── B3: 執行失敗通知 ─────────────────────────────────────────────────────────────

def send_execution_failed_notification(strategy: dict, reason: str, reject_errors: list = None) -> bool:
    """三腿下單後因超時、API 拒絕或 RPC 無回應而失敗時，發送 Telegram 通知。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    reason_label = {
        'timeout':            '⏰ 成交超時（部分腿未在規定時間內成交）',
        'exchange_rejected':  '🚫 交易所拒絕（訂單被 Deribit 回報 error）',
        'rpc_timeout':        '📡 RPC 無回應（本地 WebSocket 未收到下單回應，疑似殭屍連線）',
        # 向後相容：舊的 api_rejected 標籤繼續能顯示
        'api_rejected':       '🚫 API 拒絕（部分腿下單被交易所拒絕）',
    }.get(reason, reason)

    # RPC timeout 情境下，因本地沒收到回應，無法百分之百確定是否有單真的送達，
    # 需要特別警告使用者手動核對倉位。
    extra_warning = ''
    if reason == 'rpc_timeout':
        extra_warning = (
            "\n⚠️ *注意*：本地未收到交易所回應，機器人已嘗試強制重連並驗證倉位。"
            "若仍不放心，*請登入 Deribit 後台人工確認倉位是否與預期一致*。"
        )

    message = f"""
⚠️ *套利執行失敗* ⚠️

機器人嘗試執行以下套利交易，但未能完成，已自動撤單並驗證倉位。

*策略*: {strategy.get('strategyName', 'N/A')}
*履約價*: `${strategy.get('strike', 'N/A')}`
*到期日*: {strategy.get('expiryDate', 'N/A')}
*預估淨利*: `${strategy.get('netProfit', 0):.2f}`

*失敗原因*: {reason_label}{"".join(f"\n  • `{e}`" for e in (reject_errors or []))}{extra_warning}

_時間: {timestamp}_
""".strip()
    logger.warning(f"⚠️ 執行失敗通知: {reason}")
    return _send_message(message)


# ── A1: 緊急平倉失敗 ─────────────────────────────────────────────────────────────

def send_emergency_close_failed_notification(instrument: str, size: float, direction: str) -> bool:
    """緊急平倉失敗時發送 Telegram 警報，需立即手動處理。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    message = f"""
🚨 *緊急警報：平倉失敗，需立即手動處理！* 🚨

機器人嘗試緊急平倉以下部位，但 *失敗*，現有裸倉風險！

*未平倉詳情*:
  • *合約*: `{instrument}`
  • *倉位大小*: `{size}`
  • *平倉方向*: `{direction}`

⚠️ *請立即登入 Deribit 後台手動平倉！*

_時間: {timestamp}_
""".strip()
    logger.error(f"🚨 緊急平倉失敗通知: {instrument}")
    return _send_message(message)


# ── A2: 倉位核對異常 ─────────────────────────────────────────────────────────────

def send_position_mismatch_notification(expected: dict, actual: List[dict]) -> bool:
    """bot 狀態與 Deribit 實際倉位不符時發送 Telegram 警報。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    actual_lines = '\n'.join(
        f"  • `{p.get('instrument_name')}` size=`{p.get('size', 0)}`"
        for p in actual if abs(p.get('size', 0)) > 0
    ) or '  （無持倉）'
    message = f"""
⚠️ *倉位核對異常！* ⚠️

機器人內部狀態與 Deribit 實際倉位不符，可能存在裸倉或缺漏倉位。

*機器人預期持有*:
  • Call: `{expected.get('call_instrument', 'N/A')}`
  • Put: `{expected.get('put_instrument', 'N/A')}`
  • Perp: `BTC-PERPETUAL`

*Deribit 實際 BTC 持倉*:
{actual_lines}

⚠️ *請立即檢查 Deribit 帳戶！*

_核對時間: {timestamp}_
""".strip()
    logger.warning("⚠️ 倉位核對異常通知已發送")
    return _send_message(message)


# ── A3: WebSocket 斷線（含 30 分鐘冷卻）────────────────────────────────────────

_last_ws_disconnect_notify: float = 0.0
_WS_DISCONNECT_COOLDOWN = 1800  # 30 分鐘


def send_ws_disconnected_notification() -> bool:
    """WebSocket 斷線時發送 Telegram 警報（30 分鐘內只發一次）。"""
    global _last_ws_disconnect_notify
    now = time.time()
    if now - _last_ws_disconnect_notify < _WS_DISCONNECT_COOLDOWN:
        logger.info("⏳ WS 斷線通知冷卻中，跳過發送")
        return False
    _last_ws_disconnect_notify = now

    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    message = f"""
🔌 *WebSocket 斷線警報* 🔌

機器人與 Deribit 的 WebSocket 連線已中斷，正在嘗試重新連接...

_時間: {timestamp}_
""".strip()
    logger.warning("⚠️ WebSocket 斷線 Telegram 通知")
    return _send_message(message)


def send_ws_reconnected_notification() -> bool:
    """WebSocket 重新連線成功時發送 Telegram 通知（每次重連都通知）。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    message = f"""
✅ *WebSocket 已重新連線* ✅

機器人已成功重新連接 Deribit，恢復正常掃描運作。

_時間: {timestamp}_
""".strip()
    logger.info("✅ WebSocket 重連通知已發送")
    return _send_message(message)