# arbitrage_bot/notifications.py

"""
負責發送所有類型的通知，例如 Telegram。
"""
import requests
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict

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
    """發送成功執行交易的通知"""
    timestamp = _now_tw().strftime('%Y-%m-%d %H:%M:%S') + ' UTC+8'

    message = f"""
🚀 *套利交易已成功執行* 🚀

*策略詳情*:
  • *類型*: {opportunity['strategyName']}
  • *履約價*: `${opportunity['strike']}`
  • *到期日*: {opportunity['expiryDate']}
  • *交易單位*: `{Config.TRADE_AMOUNT_BTC} BTC`

*財務預估*:
  • *預估淨利潤*: `${opportunity['netProfit']:.2f}`
  • *理論毛利*: `${opportunity['grossProfit']:.2f}`

*狀態*:
  • *執行時間*: {timestamp}
  • *後續操作*: 永續合約將在期權到期前自動平倉，到期後恢復掃描。

✅ *交易已送出，請至 Deribit 後台確認成交狀態。*
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