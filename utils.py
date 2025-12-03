# arbitrage_bot/utils.py

"""
存放專案中可共用的工具函式與類別。
"""
import logging
import time
from typing import Optional
# --- 新增導入 ---
from logging.handlers import TimedRotatingFileHandler

# --- 新增：一個自定義過濾器，只允許包含特定關鍵字的日誌通過 ---
class OpportunityFilter(logging.Filter):
    """
    這個過濾器只允許訊息中包含 '[OPPORTUNITY]' 的日誌紀錄通過。
    """
    def filter(self, record):
        return '[OPPORTUNITY]' in record.getMessage()

def setup_logging():
    """
    設定全域的日誌格式與輸出。
    - 主日誌 (arbitrage_bot.log): 每4小時輪替一次，記錄所有 INFO 等級以上的訊息。
    - 機會日誌 (opportunities.log): 每4小時輪替一次，僅記錄包含 '[OPPORTUNITY]' 的訊息。
    - 控制台輸出: 即時顯示所有 INFO 等級以上的訊息。
    """
    # 獲取根日誌記錄器
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 如果已經有處理器，先清除，避免重複添加
    if logger.hasHandlers():
        logger.handlers.clear()

    # --- 1. 設定主日誌檔案處理器 (每4小時輪替) ---
    # atTime, when='H', interval=4, backupCount=42 (保留7天)
    main_handler = TimedRotatingFileHandler(
        'arbitrage_bot.log',
        when='H',
        interval=4,
        backupCount=42, # 4小時一次，一天6份，保留7天
        encoding='utf-8'
    )
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    main_handler.setFormatter(formatter)
    
    # --- 2. 設定機會日誌檔案處理器 (專門記錄機會) ---
    opportunity_handler = TimedRotatingFileHandler(
        'opportunities.log',
        when='H',
        interval=4,
        backupCount=42,
        encoding='utf-8'
    )
    # 機會日誌可以使用更簡潔的格式
    opportunity_formatter = logging.Formatter('%(asctime)s - %(message)s')
    opportunity_handler.setFormatter(opportunity_formatter)
    
    # 關鍵：為這個處理器添加我們自訂的過濾器
    opportunity_handler.addFilter(OpportunityFilter())

    # --- 3. 設定控制台輸出處理器 ---
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    
    # --- 4. 將所有處理器添加到根日誌記錄器 ---
    logger.addHandler(main_handler)
    logger.addHandler(opportunity_handler)
    logger.addHandler(stream_handler)


class CacheManager:
    """
    一個簡單的記憶體內緩存管理器。
    """
    def __init__(self):
        self.cache = {}
        self.cache_time = {}

    def get(self, key: str, max_age: int = 60) -> Optional[any]:
        """獲取緩存，如果過期或不存在則返回 None"""
        if key in self.cache:
            age = time.time() - self.cache_time.get(key, 0)
            if age < max_age:
                return self.cache[key]
        return None

    def set(self, key: str, value: any):
        """設置緩存"""
        self.cache[key] = value
        self.cache_time[key] = time.time()

    def clear(self, key: str = None):
        """清除指定或所有緩存"""
        if key:
            self.cache.pop(key, None)
            self.cache_time.pop(key, None)
        else:
            self.cache.clear()
            self.cache_time.clear()