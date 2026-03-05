# arbitrage_bot/deribit_ws_client.py

"""
管理與 Deribit 的 WebSocket 連線。

功能：
  - 公開頻道：訂閱 ticker（raw 每次更新）
  - 私有頻道：認證後下單、訂閱訂單狀態
  - 事件驅動：收到 ticker 更新時呼叫 on_ticker_update callback

Fix #2  重連後重新訂閱所有頻道（subscribed_instruments 在每次連線前重置）
Fix #4  斷線時將所有等待中的 RPC Future 標記為 ConnectionError，避免 thread 卡 8s
Fix #6  _next_id 加鎖，避免高頻下單時 ID 碰撞
Fix #7  pending_subscriptions 存取全面加鎖（_sub_lock）
"""

import asyncio
import websockets
import json
import time
import logging
import threading
from typing import Dict, List, Optional, Callable
from concurrent.futures import Future

from config import Config

logger = logging.getLogger(__name__)


class DeribitWebSocket:
    def __init__(self):
        self.ws = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.is_running = False

        # 公開數據快取（線程安全）
        self.ticker_data: Dict[str, dict] = {}
        self.data_lock = threading.Lock()
        self.last_update_time: Dict[str, float] = {}

        # 訂閱管理
        self._sub_lock = threading.Lock()                    # Fix #7: 保護以下兩個 set
        self.subscribed_instruments: set = set()
        self.pending_subscriptions: set = set()

        self.is_connected = False
        self.is_authenticated = False
        self.connection_ready = threading.Event()

        # 統計
        self.message_count = 0
        self.last_message_time = 0.0

        # 事件驅動：ticker 更新時的 callback（由 main.py 注入）
        self._on_ticker_update: Optional[Callable[[str], None]] = None

        # 私有 API：等待中的 RPC 請求 {id: Future}
        self._pending_requests: Dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._id_lock = threading.Lock()                    # Fix #6: 保護 _request_id
        self._request_id = 0

    # ─── 公開介面 ──────────────────────────────────────────────────────────────

    def set_on_ticker_update(self, callback: Callable[[str], None]) -> None:
        """注入 ticker 更新 callback，每次收到新 ticker 時觸發"""
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
        self.is_running = False
        self.is_connected = False
        self.connection_ready.clear()
        self._flush_pending_requests()
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
        logger.info('✅ WebSocket 已停止')

    def subscribe_instruments(self, instruments: List[str]) -> bool:
        """訂閱合約 ticker（smart，只訂閱新增的）"""
        with self._sub_lock:                                 # Fix #7
            new_instruments = [i for i in instruments if i not in self.subscribed_instruments]
        if not new_instruments:
            return True

        logger.info(f'📡 訂閱 {len(new_instruments)} 個新合約')
        channels = [f'ticker.{i}.raw' for i in new_instruments]

        if self.is_connected and self.loop:
            future = asyncio.run_coroutine_threadsafe(
                self._subscribe_channels(channels), self.loop
            )
            try:
                result = future.result(timeout=5)
                if result:
                    with self._sub_lock:                     # Fix #7
                        self.subscribed_instruments.update(new_instruments)
                return result
            except Exception as e:
                logger.error(f'❌ 訂閱失敗: {e}')
                return False
        else:
            with self._sub_lock:                             # Fix #7
                self.pending_subscriptions.update(channels)
            return False

    def get_ticker(self, instrument_name: str) -> Optional[dict]:
        with self.data_lock:
            return self.ticker_data.get(instrument_name)

    def is_data_ready(self, instruments: List[str]) -> bool:
        with self.data_lock:
            for inst in instruments:
                if inst not in self.ticker_data:
                    return False
                if time.time() - self.last_update_time.get(inst, 0) > 10:
                    return False
        return True

    def wait_for_connection(self, timeout: float = 10) -> bool:
        return self.connection_ready.wait(timeout=timeout)

    def wait_for_data(self, instruments: List[str], timeout: float = 10) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.is_data_ready(instruments):
                return True
            time.sleep(0.1)
        return False

    # ─── 私有 API：下單（WebSocket RPC）───────────────────────────────────────

    def send_order(self, direction: str, instrument: str, amount: float, price: float,
                   order_type: str = 'limit', post_only: bool = False) -> Optional[dict]:
        """
        透過 WebSocket 送出單筆訂單（同步阻塞，等待回應）。
        direction: 'buy' | 'sell'
        回傳交易所回應 dict，失敗回傳 None。
        """
        if not self.is_authenticated:
            logger.error('❌ WebSocket 尚未認證，無法下單')
            return None

        params: dict = {
            'instrument_name': instrument,
            'amount': amount,
            'type': order_type,
            'price': price,
        }
        if post_only:
            params['post_only'] = True

        return self._rpc_call(f'private/{direction}', params, timeout=8)

    def cancel_order(self, order_id: str) -> Optional[dict]:
        """透過 WebSocket 取消訂單"""
        if not self.is_authenticated:
            return None
        return self._rpc_call('private/cancel', {'order_id': order_id}, timeout=8)

    def get_position_ws(self, instrument: str) -> Optional[dict]:
        """透過 WebSocket 查詢倉位"""
        if not self.is_authenticated:
            return None
        return self._rpc_call('private/get_position', {'instrument_name': instrument}, timeout=8)

    def get_open_orders_ws(self, instrument: str) -> Optional[list]:
        """透過 WebSocket 查詢掛單"""
        if not self.is_authenticated:
            return None
        result = self._rpc_call('private/get_open_orders_by_instrument',
                                {'instrument_name': instrument}, timeout=8)
        return result if isinstance(result, list) else []

    def subscribe_user_orders(self, instrument: str,
                              callback: Callable[[dict], None]) -> None:
        """
        訂閱私有訂單更新頻道 user.orders.{instrument}.raw。
        每次訂單狀態變化時呼叫 callback(order_data)。
        """
        self._user_order_callbacks = getattr(self, '_user_order_callbacks', {})
        self._user_order_callbacks[instrument] = callback
        channel = f'user.orders.{instrument}.raw'
        if self.is_connected and self.loop:
            asyncio.run_coroutine_threadsafe(
                self._subscribe_channels([channel]), self.loop
            )
        else:
            with self._sub_lock:                             # Fix #7
                self.pending_subscriptions.add(channel)

    def get_statistics(self) -> dict:
        with self.data_lock:
            return {
                'connected': self.is_connected,
                'authenticated': self.is_authenticated,
                'subscribed_instruments': len(self.subscribed_instruments),
                'instruments_with_data': len(self.ticker_data),
                'message_count': self.message_count,
                'last_message_age': time.time() - self.last_message_time if self.last_message_time else -1,
            }

    # ─── 內部：事件循環 ────────────────────────────────────────────────────────

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
        # ── Fix #2: 每次連線前，將已訂閱頻道移回 pending 以確保重連後重新訂閱 ──
        with self._sub_lock:
            for inst in self.subscribed_instruments:
                self.pending_subscriptions.add(f'ticker.{inst}.raw')
            self.subscribed_instruments.clear()
            # 私有頻道：從 callback 登記表重建
            for inst in getattr(self, '_user_order_callbacks', {}):
                self.pending_subscriptions.add(f'user.orders.{inst}.raw')

        async with websockets.connect(
            Config.DERIBIT_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self.ws = ws
            self.is_connected = True
            logger.info('✅ WebSocket 已連接到 Deribit')

            # 1. 認證
            await self._authenticate()

            # 2. 重訂閱所有頻道（含重連後需要復原的）──────────────────────────
            with self._sub_lock:
                pending = list(self.pending_subscriptions)
            if pending:
                success = await self._subscribe_channels(pending)
                if success:
                    with self._sub_lock:
                        for ch in pending:
                            self.pending_subscriptions.discard(ch)
                            # 更新 subscribed_instruments（ticker 頻道）
                            if ch.startswith('ticker.') and ch.endswith('.raw'):
                                inst = ch[len('ticker.'):-len('.raw')]
                                self.subscribed_instruments.add(inst)

            self.connection_ready.set()

            await asyncio.gather(
                self._heartbeat(),
                self._receive_messages(),
            )

        # ── 斷線清理 ────────────────────────────────────────────────────────────
        self.is_connected = False
        self.is_authenticated = False
        self.connection_ready.clear()
        self._flush_pending_requests()                       # Fix #4
        logger.warning('⚠️ WebSocket 連線已關閉')

    async def _authenticate(self) -> None:
        """使用 client_credentials 認證私有 API"""
        payload = {
            'jsonrpc': '2.0',
            'id': self._next_id(),
            'method': 'public/auth',
            'params': {
                'grant_type': 'client_credentials',
                'client_id': Config.DERIBIT_CLIENT_ID,
                'client_secret': Config.DERIBIT_CLIENT_SECRET,
            },
        }
        await self.ws.send(json.dumps(payload))
        # 等待認證回應（最多 10 秒，增加容忍度）
        deadline = time.time() + 10
        while not self.is_authenticated and time.time() < deadline:
            await asyncio.sleep(0.05)
        if self.is_authenticated:
            logger.info('🔐 WebSocket 私有 API 認證成功')
        else:
            logger.error('❌ WebSocket 認證超時，私有 API 不可用')

    async def _heartbeat(self) -> None:
        while self.is_connected:
            await asyncio.sleep(Config.WS_HEARTBEAT_INTERVAL)
            if self.ws:
                try:
                    await self.ws.send(json.dumps({
                        'jsonrpc': '2.0',
                        'id': self._next_id(),
                        'method': 'public/test',
                    }))
                except Exception as e:
                    logger.error(f'❌ 心跳失敗: {e}')
                    break

    async def _receive_messages(self) -> None:
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                await self._handle_message(data)
                self.message_count += 1
                self.last_message_time = time.time()
        except Exception as e:
            logger.error(f'❌ 接收訊息錯誤: {e}')

    async def _handle_message(self, data: dict) -> None:
        # ── RPC 回應（有 id）──────────────────────────────────────────────────
        if 'id' in data:
            msg_id = data['id']

            # 認證回應
            if 'result' in data and isinstance(data['result'], dict):
                if 'access_token' in data['result']:
                    self.is_authenticated = True
                    return

            # 一般 RPC 回應
            with self._pending_lock:
                fut = self._pending_requests.pop(msg_id, None)
            if fut and not fut.done():
                if 'error' in data:
                    fut.set_exception(RuntimeError(str(data['error'])))
                else:
                    fut.set_result(data.get('result'))
            return

        # ── 訂閱推送（有 params）─────────────────────────────────────────────
        if 'params' not in data:
            return

        channel: str = data['params'].get('channel', '')
        payload = data['params'].get('data')

        # Ticker 更新
        if channel.startswith('ticker.') and payload:
            instrument = payload.get('instrument_name')
            if instrument:
                with self.data_lock:
                    is_new = instrument not in self.ticker_data
                    self.ticker_data[instrument] = payload
                    self.last_update_time[instrument] = time.time()
                if is_new:
                    logger.info(f'📊 首次接收 {instrument} 數據')
                # 事件驅動：通知主循環
                if self._on_ticker_update:
                    try:
                        self._on_ticker_update(instrument)
                    except Exception as e:
                        logger.error(f'❌ on_ticker_update callback 錯誤: {e}')

        # 私有訂單更新
        elif channel.startswith('user.orders.'):
            callbacks = getattr(self, '_user_order_callbacks', {})
            if isinstance(payload, list):
                for order in payload:
                    inst = order.get('instrument_name', '')
                    cb = callbacks.get(inst)
                    if cb:
                        try:
                            cb(order)
                        except Exception as e:
                            logger.error(f'❌ user_order callback 錯誤: {e}')

    # ─── 內部工具 ──────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._id_lock:                                  # Fix #6
            self._request_id += 1
            return self._request_id

    def _flush_pending_requests(self) -> None:
        """Fix #4: 斷線時將所有等待中的 RPC Future 標記為失敗，避免 thread 卡 8s。"""
        with self._pending_lock:
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.set_exception(ConnectionError('WebSocket disconnected'))
            self._pending_requests.clear()

    async def _subscribe_channels(self, channels: List[str]) -> bool:
        if not self.ws or not self.is_connected:
            with self._sub_lock:                             # Fix #7
                self.pending_subscriptions.update(channels)
            return False
        try:
            # 公開頻道用 public/subscribe，私有用 private/subscribe
            public = [c for c in channels if not c.startswith('user.')]
            private = [c for c in channels if c.startswith('user.')]

            if public:
                await self.ws.send(json.dumps({
                    'jsonrpc': '2.0', 'id': self._next_id(),
                    'method': 'public/subscribe',
                    'params': {'channels': public},
                }))
            if private and self.is_authenticated:
                await self.ws.send(json.dumps({
                    'jsonrpc': '2.0', 'id': self._next_id(),
                    'method': 'private/subscribe',
                    'params': {'channels': private},
                }))
            elif private and not self.is_authenticated:
                # 認證尚未完成，留待重試
                with self._sub_lock:
                    self.pending_subscriptions.update(private)
                logger.warning('⚠️ 私有頻道訂閱延遲：等待認證')
            return True
        except Exception as e:
            logger.error(f'❌ 訂閱失敗: {e}')
            return False

    def _rpc_call(self, method: str, params: dict, timeout: float = 8) -> Optional[dict]:
        """同步阻塞的 WebSocket RPC 呼叫"""
        if not self.loop or not self.is_connected:
            return None

        msg_id = self._next_id()
        fut: Future = Future()

        with self._pending_lock:
            self._pending_requests[msg_id] = fut

        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': msg_id,
            'method': method,
            'params': params,
        })

        asyncio.run_coroutine_threadsafe(self.ws.send(payload), self.loop)

        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.error(f'❌ RPC {method} 失敗: {e}')
            with self._pending_lock:
                self._pending_requests.pop(msg_id, None)
            return None
