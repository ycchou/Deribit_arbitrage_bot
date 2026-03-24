# arbitrage_bot/global_state.py

"""
GlobalState：交易引擎的執行期共用狀態（單例）。
包含：掃描節流、失敗冷卻計時、交易互斥鎖、每日交易計數。
"""

import threading
import time
import logging
from datetime import datetime, timezone, timedelta

_TZ_TAIPEI = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


class GlobalState:
    def __init__(self):
        self.current_instruments: set  = set()
        self.last_expiry_date: str     = None
        self.last_failure_time: float  = 0.0

        # 節流：避免同一時間連發多次掃描（最快 50ms 一次）
        self._last_scan_time: float    = 0.0
        self.MIN_SCAN_INTERVAL: float  = 0.05

        # Fix #1：交易互斥鎖，確保同一時間只有一個 thread 能執行交易
        self._trade_lock = threading.Lock()

        # 每日交易計數
        self.daily_trade_count: int  = 0
        self.last_trade_time: float  = 0.0
        self._trade_date: str        = ''   # 格式 'YYYY-MM-DD'（UTC+8）

    def should_scan(self) -> bool:
        """節流檢查：距上次掃描是否超過最小間隔。"""
        now = time.time()
        if now - self._last_scan_time < self.MIN_SCAN_INTERVAL:
            return False
        self._last_scan_time = now
        return True

    def check_and_reset_daily(self) -> None:
        """若目前日期（UTC+8）與 _trade_date 不同則清零每日計數。"""
        today = datetime.now(_TZ_TAIPEI).strftime('%Y-%m-%d')
        if self._trade_date != today:
            self._trade_date = today
            self.daily_trade_count = 0
            logger.info(f"📅 每日計數器重置 (日期: {today})")


# 全域單例，由 scan_orchestrator 與 trading_engine 共用
global_state = GlobalState()
