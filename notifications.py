# arbitrage_bot/notifications.py

"""
負責發送所有類型的通知，例如 Telegram。
"""
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict

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
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' UTC'
    
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
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' UTC'
    cooldown_hours = Config.COOLDOWN_PERIOD_SECONDS / 3600
    next_trade_time = (datetime.utcnow() + timedelta(seconds=Config.COOLDOWN_PERIOD_SECONDS)).strftime('%Y-%m-%d %H:%M')
    
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
  • *後續操作*: 永續合約將在期權到期前自動平倉。
  • *冷卻期*: 機器人已進入 **{cooldown_hours:.0f} 小時**冷卻期。
  • *下次可交易時間*: `~{next_trade_time} UTC`

✅ *交易已送出，請至 Deribit 後台確認成交狀態。*
""".strip()
    
    logger.info(f"發送交易執行成功通知 (履約價 ${opportunity['strike']})")
    return _send_message(message)

def send_liquidity_issue_notification(opportunity: Dict) -> bool:
    """發送因流動性不足而放棄交易的通知"""
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' UTC'
    
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