# arbitrage_bot/ws_core.py

"""
DeribitWebSocket：主 WebSocket 客戶端類別。
透過 Mixin 組合三個獨立模組的能力：
  WsRpcMixin          (ws_rpc.py)           — RPC 呼叫與訂單操作
  WsSubscriptionMixin (ws_subscription.py)  — 頻道訂閱管理
  WsMessageHandlerMixin(ws_message_handler.py) — 訊息路由

此類別自身負責：連線生命週期、認證、心跳、接收迴圈。
"""

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import websockets

from config import Config
from ws_rpc             import WsRpcMixin
from ws_subscription    import WsSubscriptionMixin
from ws_message_handler import WsMessageHandlerMixin

logger = logging.getLogger(__name__)


class DeribitWebSocket(WsRpcMixin, WsSubscriptionMixin, WsMessageHandlerMixin):

    def __init__(self):
        # ── 連線核心狀態 ─────────────────────────────────────────────────────
        self.ws            = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread]        = None
        self.is_running    = False
        self.is_connected  = False
        self.is_authenticated = False
        self.connection_ready = threading.Event()

        # ── 公開數據快取（線程安全）────────────────────────────────────────────
        self.ticker_data: Dict[str, dict]  = {}
        self.data_lock   = threading.Lock()
        self.last_update_time: Dict[str, float] = {}

        # ── 統計 ────────────────────────────────────────────────────────────
        self.message_count    = 0
        self.last_message_time = 0.0

        # ── 事件驅動 callback ─────────────────────────────────────────────
        self._on_ticker_update: Optional[Callable[[str], None]] = None

        # ── 初始化各 Mixin 的私有狀態 ────────────────────────────────────────
        self._init_rpc_state()
        self._init_subscription_state()

    # ── 公開介面 ────────────────────────────────────────────────────────────────

    def set_on_ticker_update(self, callback: Callable[[str], None]) -> None:
        self._on_ticker_update = callback

    def start(self) -> None:
        if self.is_running:
            logger.warning('WebSocket 已在運行中')
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()
        logger.info('✅ WebSocket 線程已啟動')

    def stop(self) -> None:
        logger.info('🛑 正在停止 WebSocket...')
        self.is_running    = False
        self.is_connected  = False
        self.connection_ready.clear()
        self._flush_pending_requests()
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
        logger.info('✅ WebSocket 已停止')

    def get_ticker(self, instrument_name: str) -> Optional[dict]:
        with self.data_lock:
            return self.ticker_data.get(instrument_name)

    def get_tickers(self, instrument_names: list) -> dict:
        """一次取得多個合約的 ticker（單次加鎖，降低鎖競爭）。"""
        with self.data_lock:
            return {name: self.ticker_data.get(name) for name in instrument_names}

    def is_data_ready(self, instruments) -> bool:
        with self.data_lock:
            for inst in instruments:
                if inst not in self.ticker_data:
                    return False
                if time.time() - self.last_update_time.get(inst, 0) > 10:
                    return False
        return True

    def wait_for_connection(self, timeout: float = 10) -> bool:
        return self.connection_ready.wait(timeout=timeout)

    def wait_for_data(self, instruments, timeout: float = 10) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.is_data_ready(instruments):
                return True
            time.sleep(0.1)
        return False

    def get_statistics(self) -> dict:
        with self.data_lock:
            return {
                'connected':               self.is_connected,
                'authenticated':           self.is_authenticated,
                'subscribed_instruments':  len(self.subscribed_instruments),
                'instruments_with_data':   len(self.ticker_data),
                'message_count':           self.message_count,
                'last_message_age':        (
                    time.time() - self.last_message_time
                    if self.last_message_time else -1
                ),
            }

    # ── 事件循環 ────────────────────────────────────────────────────────────────

    def _run_event_loop(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        while self.is_running:
            try:
                self.loop.run_until_complete(self._connect_and_run())
            except Exception as e:
                logger.error(f'❌ WebSocket 事件循環錯誤: {e}')
                self.connection_ready.clear()
                if self.is_running:
                    logger.info(f'⏳ {Config.WS_RECONNECT_DELAY}s 後重連...')
                    time.sleep(Config.WS_RECONNECT_DELAY)

    async def _connect_and_run(self) -> None:
        # Fix #2: 重連前將已訂閱頻道移回 pending
        self._rebuild_pending_on_reconnect()

        async with websockets.connect(
            Config.DERIBIT_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self.ws           = ws
            self.is_connected = True
            logger.info('✅ WebSocket 已連接到 Deribit')

            await self._authenticate()
            await self._flush_pending_subscriptions()
            self.connection_ready.set()

            await asyncio.gather(
                self._heartbeat(),
                self._receive_messages(),
            )

        # 斷線清理
        self.is_connected     = False
        self.is_authenticated = False
        self.connection_ready.clear()
        self._flush_pending_requests()   # Fix #4
        logger.warning('⚠️ WebSocket 連線已關閉')

    # ── 認證 ────────────────────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        payload = {
            'jsonrpc': '2.0',
            'id': self._next_id(),
            'method': 'public/auth',
            'params': {
                'grant_type':    'client_credentials',
                'client_id':     Config.DERIBIT_CLIENT_ID,
                'client_secret': Config.DERIBIT_CLIENT_SECRET,
            },
        }
        await self.ws.send(json.dumps(payload))
        deadline = time.time() + 10
        while not self.is_authenticated and time.time() < deadline:
            await asyncio.sleep(0.05)
        if self.is_authenticated:
            logger.info('🔐 WebSocket 私有 API 認證成功')
        else:
            logger.error('❌ WebSocket 認證超時，私有 API 不可用')

    # ── 心跳 & 接收 ────────────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        while self.is_connected:
            await asyncio.sleep(Config.WS_HEARTBEAT_INTERVAL)
            if self.ws:
                try:
                    await self.ws.send(json.dumps({
                        'jsonrpc': '2.0',
                        'id':      self._next_id(),
                        'method':  'public/test',
                    }))
                except Exception as e:
                    logger.error(f'❌ 心跳失敗: {e}')
                    break

    async def _receive_messages(self) -> None:
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                await self._handle_message(data)
                self.message_count    += 1
                self.last_message_time = time.time()
        except Exception as e:
            logger.error(f'❌ 接收訊息錯誤: {e}')
