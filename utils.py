# arbitrage_bot/utils.py

"""
存放專案中可共用的工具函式與類別。
"""
import logging
import time
import datetime
from typing import Optional
from logging.handlers import TimedRotatingFileHandler

_TZ_TAIPEI = datetime.timezone(datetime.timedelta(hours=8))


class _TaipeiFormatter(logging.Formatter):
    """Log formatter that displays timestamps in UTC+8."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=_TZ_TAIPEI)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S') + ',%03d' % record.msecs

# --- 新增：一個自定義過濾器，只允許包含特定關鍵字的日誌通過 ---
class OpportunityFilter(logging.Filter):
    """
    這個過濾器只允許訊息中包含 '[OPPORTUNITY]' 的日誌紀錄通過。
    """
    def filter(self, record):
        return '[OPPORTUNITY]' in record.getMessage()

def setup_logging(extra_handlers=None):
    """
    設定全域的日誌格式與輸出（時間戳為 UTC+8）。
    - 主日誌 (arbitrage_bot.log): 每4小時輪替一次，記錄所有 INFO 等級以上的訊息。
    - 機會日誌 (opportunities.log): 每4小時輪替一次，僅記錄包含 '[OPPORTUNITY]' 的訊息。
    - 控制台輸出: 即時顯示所有 INFO 等級以上的訊息。
    extra_handlers: 額外的 handler 列表，統一在此加入避免重複添加。
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 清除所有既有 handler，避免重複（含重啟時殘留的 handler）
    logger.handlers.clear()

    fmt = _TaipeiFormatter('%(asctime)s - %(levelname)s - %(message)s')

    # --- 1. 主日誌（每4小時輪替）---
    main_handler = TimedRotatingFileHandler(
        'arbitrage_bot.log', when='H', interval=4, backupCount=42, encoding='utf-8'
    )
    main_handler.setFormatter(fmt)

    # --- 2. 機會日誌（只記錄 [OPPORTUNITY]）---
    opportunity_handler = TimedRotatingFileHandler(
        'opportunities.log', when='H', interval=4, backupCount=42, encoding='utf-8'
    )
    opportunity_handler.setFormatter(_TaipeiFormatter('%(asctime)s - %(message)s'))
    opportunity_handler.addFilter(OpportunityFilter())

    # --- 3. 控制台輸出 ---
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.addHandler(main_handler)
    logger.addHandler(opportunity_handler)
    logger.addHandler(stream_handler)

    # --- 4. 額外 handler（如 BotStateLogHandler）統一在此加入 ---
    for h in (extra_handlers or []):
        logger.addHandler(h)


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