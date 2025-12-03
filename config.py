# arbitrage_bot/config.py

"""
存放所有靜態設定值。
將設定與程式邏輯分離，方便管理與修改。
"""

class Config:
    # 建議未來將金鑰存放在環境變數中，而不是寫死在程式碼裡
    DERIBIT_CLIENT_ID = ''
    DERIBIT_CLIENT_SECRET = ''
    TELEGRAM_BOT_TOKEN = ''
    TELEGRAM_CHAT_ID = ''
    
    DERIBIT_BASE_URL = 'https://test.deribit.com/api/v2'
    DERIBIT_WS_URL = 'wss://www.deribit.com/ws/api/v2'
    TELEGRAM_BASE_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'
    
    # WebSocket 設定
    WS_HEARTBEAT_INTERVAL = 10
    WS_RECONNECT_DELAY = 5
    
    # 緩存設定
    FUNDING_RATE_CACHE_SECONDS = 3600  # 資金費率緩存60分鐘
    EXPIRY_CACHE_SECONDS = 3600      # 到期日緩存1小時
    
    # --- 新增：交易與風險控制設定 ---
    TRADE_AMOUNT_BTC = 0.3  # 單次交易單位固定為 1 BTC
    COOLDOWN_PERIOD_SECONDS = 28 * 60 * 60  # 28小時冷卻期
    
    # 部位管理設定
    POSITION_CLOSE_TRIGGER_SECONDS = 60  # 在到期前60秒觸發平倉
    TAKER_FORCE_CLOSE_SECONDS = 10       # 在到期前10秒強制Taker平倉
    
    # --- 新增設定 (根據您的要求) ---
    SCAN_INTERVAL_SECONDS = 1  # 主循環每次掃描後的等待時間（秒）
    MIN_NET_PROFIT_OPPORTUNITY = 20  # 認定為高利潤機會的最低淨利潤（美元）