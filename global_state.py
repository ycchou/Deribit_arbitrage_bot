# arbitrage_bot/global_state.py

"""
GlobalState：交易引擎的執行期共用狀態（單例）。
包含：冷卻計時器、掃描節流鎖、交易互斥鎖。
"""

import threading
import time


class GlobalState:
    def __init__(self):
        self.current_instruments: set  = set()
        self.last_expiry_date: str     = None
        self.last_trade_time: float    = 0.0
        self.last_failure_time: float  = 0.0

        # 節流：避免同一時間連發多次掃描（最快 50ms 一次）
        self._last_scan_time: float    = 0.0
        self.MIN_SCAN_INTERVAL: float  = 0.05

        # Fix #1：交易互斥鎖，確保同一時間只有一個 thread 能執行交易
        self._trade_lock = threading.Lock()

    def should_scan(self) -> bool:
        """節流檢查：距上次掃描是否超過最小間隔。"""
        now = time.time()
        if now - self._last_scan_time < self.MIN_SCAN_INTERVAL:
            return False
        self._last_scan_time = now
        return True


# 全域單例，由 scan_orchestrator 與 trading_engine 共用
global_state = GlobalState()
