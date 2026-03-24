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
• *估計總手續費*: `${opportunity['totalFees']:.2f}` (含永續合約平倉費)
• *預估資金費率*: `{funding_text}` (基於當前費率 {opportunity['fundingRate24h']:.4f}% 估算24H)
• *所需保證金 (估算)*: `${opportunity['margin']:.0f}`

⚠️ *注意*: 此策略使用永續合約，存在基差風險。永續合約部位需在期權到期時手動平倉。利潤估算已包含平倉費用。

_資料時間: {timestamp}_
_數據來源: WebSocket 實時訂閱_
""".strip()
    
    success = _send_message(message)
    if success:
        logger.info(f"✅ 成功發送 Telegram 通知 (履約價 ${opportunity['strike']})")
    return success

def send_trade_execution_notification(opportunity: Dict) -> bool:
    """發送成功執行交易的通知，含各腿實際成交價、手續費、期權到期時間。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    # ── 實際成交價（market order 後由 average_price 填入）──────────────────
    perp_fill  = opportunity.get('fill_perp_price') or opportunity.get('perpOpenPrice', 0.0)
    call_fill  = opportunity.get('fill_call_price') or opportunity.get('callPrice', 0.0)
    put_fill   = opportunity.get('fill_put_price')  or opportunity.get('putPrice',  0.0)
    amount     = Config.TRADE_AMOUNT_BTC

    # ── 手續費估算（Deribit taker，市價單）────────────────────────────────
    # 選擇權：0.03% of underlying per leg
    # 永續：  0.05% of notional
    opt_fee  = perp_fill * 0.0003 * amount   # per option leg (USD)
    perp_fee = perp_fill * 0.0005 * amount   # perp leg (USD)
    entry_fees_total = opt_fee * 2 + perp_fee

    # ── 成交價 USD 換算（選擇權報 BTC，乘以 perp 成交價）─────────────────
    call_usd = call_fill * perp_fill
    put_usd  = put_fill  * perp_fill

    # ── 期權到期時間（UTC+8）──────────────────────────────────────────────
    expiry_ts = opportunity.get('expiryTimestamp', 0)
    if expiry_ts:
        # expiryTimestamp 是毫秒，需除以 1000 轉為秒
        expiry_dt  = datetime.fromtimestamp(expiry_ts / 1000, tz=_TZ_TAIPEI)
        expiry_str = expiry_dt.strftime('%Y-%m-%d %H:%M') + ' UTC+8'
    else:
        expiry_str = opportunity.get('expiryDate', 'N/A')

    call_action = '買入' if opportunity['callDirection'] == 'buy' else '賣出'
    put_action  = '買入' if opportunity['putDirection']  == 'buy' else '賣出'
    perp_action = '賣出' if opportunity['perpDirection'] == 'short' else '買入'

    message = f"""
🚀 *套利交易已成功執行* 🚀

*{opportunity['strategyName']}*  履約價 `${opportunity['strike']}`  數量 `{amount} BTC`

--- *成交明細* ---
• *{call_action} Call* `{opportunity['callInstrument']}`
  成交價: `{call_fill:.4f} BTC` ≈ `${call_usd:.0f}` | 手續費: `${opt_fee:.2f}`
• *{put_action} Put* `{opportunity['putInstrument']}`
  成交價: `{put_fill:.4f} BTC` ≈ `${put_usd:.0f}` | 手續費: `${opt_fee:.2f}`
• *{perp_action} Perp* `BTC-PERPETUAL`
  成交價: `${perp_fill:,.0f}` | 手續費: `${perp_fee:.2f}`

--- *財務摘要* ---
• *入場手續費合計*: `${entry_fees_total:.2f}`
• *預估淨利潤*: `${opportunity['netProfit']:.2f}`（含平倉費估算）

*期權到期*: {expiry_str}
*執行時間*: {timestamp}
""".strip()

    logger.info(f"發送交易執行成功通知 (履約價 ${opportunity['strike']})")
    return _send_message(message)

def send_startup_notification() -> bool:
    """發送機器人啟動通知"""
    env = "🧪 Testnet" if Config.USE_TESTNET else "🔴 Mainnet"
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    message = f"""
🤖 *Deribit 套利機器人已啟動*

• *環境*: {env}
• *交易單位*: `{Config.TRADE_AMOUNT_BTC} BTC`
• *最低利潤門檻*: `${Config.MIN_NET_PROFIT_OPPORTUNITY}`
• *啟動時間*: {timestamp}

✅ WebSocket 已連接並認證，開始監控市場。
""".strip()
    logger.info("發送啟動通知")
    return _send_message(message)


def send_position_closed_notification(position: Dict, close_method: str) -> bool:
    """發送部位到期平倉通知，包含保證金使用與預估收益。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    entry_time  = position.get('entry_time', 0)
    duration_s  = int(datetime.utcnow().timestamp() - entry_time) if entry_time else 0
    hours, rem  = divmod(duration_s, 3600)
    minutes     = rem // 60

    net_profit  = position.get('net_profit_est', 0.0)
    margin      = position.get('margin_est', 0.0)
    profit_icon = '🟢' if net_profit >= 0 else '🔴'

    method_label = {
        'maker':   '✅ Maker 限價單成交',
        'taker':   '⚡️ Taker 強制平倉',
        'expired': '⚠️ 已過期（未正常平倉）',
    }.get(close_method, close_method)

    message = f"""
📦 *部位已平倉* 📦

*平倉方式*: {method_label}
*持倉時長*: {hours}h {minutes}m
*平倉時間*: {timestamp}

*財務摘要*:
  • *使用保證金（估算）*: `${margin:.0f}`
  • *預估淨收益*: {profit_icon} `${net_profit:.2f}`

⚠️ _收益為開倉時預估值，實際損益以 Deribit 帳戶結算為準。_
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

def send_execution_failed_notification(strategy: dict, reason: str) -> bool:
    """三腿下單後因超時或 API 拒絕而失敗時，發送 Telegram 通知。"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'
    reason_label = {
        'timeout':      '⏰ 成交超時（部分腿未在規定時間內成交）',
        'api_rejected': '🚫 API 拒絕（部分腿下單被交易所拒絕）',
    }.get(reason, reason)
    message = f"""
⚠️ *套利執行失敗* ⚠️

機器人嘗試執行以下套利交易，但未能完成，已自動撤單並緊急平倉。

*策略*: {strategy.get('strategyName', 'N/A')}
*履約價*: `${strategy.get('strike', 'N/A')}`
*到期日*: {strategy.get('expiryDate', 'N/A')}
*預估淨利*: `${strategy.get('netProfit', 0):.2f}`

*失敗原因*: {reason_label}

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