# arbitrage_bot/bot_state.py

"""
Thread-safe shared state between the trading bot and the live dashboard server.
"""

import time
import logging
import threading
import psutil
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional, Dict, List, Callable

from config import Config

_TZ_TAIPEI = timezone(timedelta(hours=8))

def _tw_time() -> str:
    return datetime.now(_TZ_TAIPEI).strftime('%H:%M:%S')

logger = logging.getLogger(__name__)


class BotStateLogHandler(logging.Handler):
    """Feeds Python log records into BotState's live log buffer."""

    def __init__(self, state: 'BotState'):
        super().__init__()
        self._state = state
        self.setFormatter(logging.Formatter('%(name)s — %(message)s'))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._state.add_log(msg, record.levelname)
        except Exception:
            pass


class BotState:
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()

        # ── Broadcast throttle（高頻資料最多每秒推一次）─────────────────────
        self._last_price_push: float = 0.0
        self._last_scan_push: float = 0.0
        self._PUSH_INTERVAL: float = 1.0

        # ── Market data ──────────────────────────────────────────────────────
        self.btc_price: float = 0.0
        self.funding_rate: float = 0.0

        # ── Connection status ────────────────────────────────────────────────
        self.ws_connected: bool = False
        self.ws_authenticated: bool = False

        # ── Scan state ───────────────────────────────────────────────────────
        self.scan_info: Dict = {}
        self.scan_count: int = 0

        # ── Position ─────────────────────────────────────────────────────────
        self.active_position: Optional[Dict] = None
        self._active_positions_list: List[Dict] = []

        # ── History ──────────────────────────────────────────────────────────
        self.trade_history: deque = deque(maxlen=50)
        self.log_buffer: deque = deque(maxlen=200)
        self.last_trade_time: float = 0.0

        # ── System metrics (sampled every ~60s, 24h window) ──────────────────
        self._cpu_history: deque = deque()  # (timestamp, pct)
        self._ram_history: deque = deque()

        # ── Trading power state ─────────────────────────────────────────────
        self.trading_enabled: bool = False   # 預設關機（不下單）

        # ── Broadcast callback (injected by live_server) ─────────────────────
        self._broadcast: Optional[Callable[[Dict], None]] = None

        # Start background metrics collector
        threading.Thread(
            target=self._collect_metrics, daemon=True, name='metrics-collector'
        ).start()

    def set_broadcast_callback(self, cb: Callable[[Dict], None]) -> None:
        self._broadcast = cb

    def update_trading_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.trading_enabled = enabled
        self._push({'type': 'trading_enabled', 'value': enabled})

    # ── Update Methods ────────────────────────────────────────────────────────

    def update_btc_price(self, price: float) -> None:
        now = time.time()
        with self._lock:
            self.btc_price = price
            should_push = now - self._last_price_push >= self._PUSH_INTERVAL
            if should_push:
                self._last_price_push = now
        if should_push:
            self._push({'type': 'btc_price', 'value': price})

    def update_funding_rate(self, rate: float) -> None:
        with self._lock:
            self.funding_rate = rate
        self._push({'type': 'funding_rate', 'value': rate})

    def update_ws_status(self, connected: bool, authenticated: bool) -> None:
        with self._lock:
            self.ws_connected = connected
            self.ws_authenticated = authenticated
        self._push({'type': 'ws_status', 'connected': connected, 'authenticated': authenticated})

    def update_scan_info(self, info: Dict) -> None:
        now = time.time()
        with self._lock:
            self.scan_info = info
            should_push = now - self._last_scan_push >= self._PUSH_INTERVAL
            if should_push:
                self._last_scan_push = now
        if should_push:
            self._push({'type': 'scan_info', 'data': info})

    def update_active_position(self, position: Optional[Dict]) -> None:
        with self._lock:
            self.active_position = position
        self._push({'type': 'active_position', 'position': position})

    def update_active_positions(self, positions: List[Dict]) -> None:
        """接受部位列表；同時維護 active_position 供 dashboard 向後相容。"""
        with self._lock:
            self.active_position = positions[0] if positions else None
            self._active_positions_list = list(positions)
        self._push({'type': 'active_positions', 'positions': positions})

    def add_trade(self, trade: Dict) -> None:
        now = time.time()
        entry = {**trade, 'time': _tw_time(), 'timestamp': now}
        with self._lock:
            self.trade_history.append(entry)
            self.last_trade_time = now
        self._push({'type': 'trade', 'trade': entry})

    def update_trade(self, call_instrument: str, **updates) -> None:
        """依 callInstrument 找到對應交易記錄並更新欄位（例如 closeMethod）。"""
        with self._lock:
            for t in reversed(self.trade_history):
                if t.get('callInstrument') == call_instrument:
                    t.update(updates)
                    break

    def add_log(self, message: str, level: str = 'INFO') -> None:
        entry = {'time': _tw_time(), 'level': level, 'message': message}
        with self._lock:
            self.log_buffer.append(entry)
        self._push({'type': 'log', **entry})

    # ── Read Methods ──────────────────────────────────────────────────────────

    def get_system_metrics(self) -> Dict:
        now = time.time()
        cpu_now = psutil.cpu_percent()
        ram = psutil.virtual_memory()
        cutoff = now - 86400  # 24h
        with self._lock:
            cpu_vals = [v for t, v in self._cpu_history if t >= cutoff]
            ram_vals = [v for t, v in self._ram_history if t >= cutoff]
        return {
            'uptime_seconds': int(now - self.start_time),
            'cpu_percent': round(cpu_now, 1),
            'cpu_peak_24h': round(max(cpu_vals, default=cpu_now), 1),
            'ram_percent': round(ram.percent, 1),
            'ram_peak_24h': round(max(ram_vals, default=ram.percent), 1),
        }

    def get_snapshot(self) -> Dict:
        system = self.get_system_metrics()  # 在取鎖前呼叫，避免 Lock 重入死鎖
        with self._lock:
            return {
                'type': 'snapshot',
                'btc_price': self.btc_price,
                'funding_rate': self.funding_rate,
                'ws_connected': self.ws_connected,
                'ws_authenticated': self.ws_authenticated,
                'scan_info': self.scan_info,
                'active_position': self.active_position,
                'active_positions': self._active_positions_list,
                'trade_history': list(self.trade_history),
                'log_buffer': list(self.log_buffer),
                'last_trade_time': self.last_trade_time,
                'system': system,
                'trading_enabled': self.trading_enabled,
                'config': Config.get_public_config(),
            }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _collect_metrics(self) -> None:
        while True:
            now = time.time()
            cpu = psutil.cpu_percent(interval=5)
            ram = psutil.virtual_memory().percent
            cutoff = now - 86400
            with self._lock:
                self._cpu_history.append((now, cpu))
                self._ram_history.append((now, ram))
                while self._cpu_history and self._cpu_history[0][0] < cutoff:
                    self._cpu_history.popleft()
                while self._ram_history and self._ram_history[0][0] < cutoff:
                    self._ram_history.popleft()
            time.sleep(55)

    def _push(self, data: Dict) -> None:
        if self._broadcast:
            try:
                self._broadcast(data)
            except Exception:
                pass


# Global singleton
bot_state = BotState()
