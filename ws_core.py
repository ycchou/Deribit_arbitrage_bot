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
from concurrent.futures import Future
from typing import Callable, Dict, Optional

import websockets

from config import Config
from ws_rpc             import WsRpcMixin
from ws_subscription    import WsSubscriptionMixin
from ws_message_handler import WsMessageHandlerMixin
from notifications import send_ws_disconnected_notification, send_ws_reconnected_notification

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
        self._has_connected_before = False  # 區分首次啟動 vs 斷線重連
        self._reconnect_count = 0
        self._disconnect_time = 0.0

        # ── 事件驅動 callback ─────────────────────────────────────────────
        self._on_ticker_update: Optional[Callable[[str], None]] = None

        # ── Auth Token 狀態 ─────────────────────────────────────────────────
        self._access_token: str    = ''
        self._refresh_token: str   = ''
        self._token_expires_at: float = 0.0

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
                'reconnect_count':         self._reconnect_count,
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
            # 斷線後重連
            self.connection_ready.clear()
            if self.is_running:
                self._reconnect_count += 1
                downtime = ''
                if self._disconnect_time > 0:
                    downtime = f'（斷線時長: {time.time() - self._disconnect_time:.1f}s）'
                logger.info(
                    f'⏳ {Config.WS_RECONNECT_DELAY}s 後重連... '
                    f'(第 {self._reconnect_count} 次重連{downtime})'
                )
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
            self._disconnect_time = 0.0
            logger.info('✅ WebSocket 已連接到 Deribit')

            # _receive_messages 必須與 _authenticate 並行執行，
            # 否則 auth response 無人讀取，導致認證永遠超時。
            receiver_task = asyncio.create_task(
                self._receive_messages(), name='receiver')

            try:
                await self._authenticate_then_subscribe()
            except Exception as e:
                logger.error(f'❌ 認證/訂閱失敗: {e}')

            # 長期運行的輔助 task
            helper_tasks = [
                asyncio.create_task(self._heartbeat(), name='heartbeat'),
                asyncio.create_task(
                    self._token_refresh_loop(), name='token_refresh'),
            ]

            # receiver 是連線存活的基準 —— 它結束代表連線已死
            try:
                await receiver_task
            finally:
                for t in helper_tasks:
                    t.cancel()
                await asyncio.gather(*helper_tasks, return_exceptions=True)

        # 斷線清理
        self.is_connected     = False
        self.is_authenticated = False
        self._disconnect_time = time.time()
        self.connection_ready.clear()
        self._flush_pending_requests()   # Fix #4
        logger.warning('⚠️ WebSocket 連線已關閉，準備重連...')
        send_ws_disconnected_notification()  # A3

    # ── 認證 ────────────────────────────────────────────────────────────────────

    async def _authenticate_then_subscribe(self) -> None:
        """認證完成後再訂閱，並設定 connection_ready。"""
        await self._authenticate()
        await self._flush_pending_subscriptions()
        self.connection_ready.set()
        if self._has_connected_before:
            send_ws_reconnected_notification()
        else:
            self._has_connected_before = True

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

    # ── Token 自動刷新 ─────────────────────────────────────────────────────────

    async def _token_refresh_loop(self) -> None:
        """定期刷新 auth token，在到期前 60 秒刷新。"""
        while self.is_connected and self.is_running:
            # 等到認證完成才開始計時
            if not self.is_authenticated:
                await asyncio.sleep(1)
                continue
            # 短間隔 sleep，確保 cancellation 和 is_connected 檢查能及時生效
            sleep_time = max(10, self._token_expires_at - time.time())
            while sleep_time > 0 and self.is_connected:
                await asyncio.sleep(min(sleep_time, 30))
                sleep_time = self._token_expires_at - time.time()
            if not self.is_connected:
                break
            logger.info('🔄 刷新 Deribit auth token...')
            refresh_token = self._refresh_token
            if not refresh_token:
                # 無 refresh token，重新完整認證
                self.is_authenticated = False
                await self._authenticate()
                continue
            payload = {
                'jsonrpc': '2.0',
                'id': self._next_id(),
                'method': 'public/auth',
                'params': {
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh_token,
                },
            }
            try:
                await self.ws.send(json.dumps(payload))
                # 回應由 _handle_rpc_response 處理，會更新 token 資訊
                await asyncio.sleep(2)
                if self.is_authenticated:
                    logger.info('✅ Token 刷新成功')
                else:
                    logger.warning('⚠️ Token 刷新失敗，嘗試重新認證')
                    await self._authenticate()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f'❌ Token 刷新異常: {e}')

    # ── 心跳 & 接收 ────────────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        """
        心跳機制：定期送出 public/test 並**等待回應**，連續 2 次無回應
        即判定為殭屍連線，主動關閉以觸發重連。

        修復原因：舊版只檢查 send() 是否成功，若 server 沒回應但 TCP 沒斷，
        會形成半死狀態，導致所有 RPC 都 timeout 但 is_connected 還是 True。
        """
        consecutive_failures = 0
        max_failures = 2
        response_timeout = 5.0

        while self.is_connected:
            await asyncio.sleep(Config.WS_HEARTBEAT_INTERVAL)
            if not self.is_connected or not self.ws:
                break

            msg_id = self._next_id()
            fut: Future = Future()
            with self._pending_lock:
                self._pending_requests[msg_id] = fut

            try:
                await self.ws.send(json.dumps({
                    'jsonrpc': '2.0',
                    'id':      msg_id,
                    'method':  'public/test',
                }))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f'❌ 心跳送出失敗: {e}')
                with self._pending_lock:
                    self._pending_requests.pop(msg_id, None)
                self.is_connected = False
                break

            # 等待 public/test 回應（在 event loop 內用 asyncio 包裝 concurrent Future）
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(fut),
                    timeout=response_timeout,
                )
                if consecutive_failures > 0:
                    logger.info(f'✅ 心跳恢復（連續失敗 {consecutive_failures} 次後復原）')
                consecutive_failures = 0
            except (asyncio.TimeoutError, Exception) as e:
                consecutive_failures += 1
                with self._pending_lock:
                    self._pending_requests.pop(msg_id, None)
                logger.warning(
                    f'⚠️ 心跳無回應 ({consecutive_failures}/{max_failures}) — '
                    f'{"timeout" if isinstance(e, asyncio.TimeoutError) else repr(e)}'
                )
                if consecutive_failures >= max_failures:
                    logger.error(
                        '❌ 心跳連續無回應 — 判定殭屍連線，主動關閉以觸發重連'
                    )
                    self.is_connected = False
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    break

    async def _receive_messages(self) -> None:
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                await self._handle_message(data)
                self.message_count    += 1
                self.last_message_time = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f'❌ 接收訊息錯誤: {e}')
        finally:
            # 安全信號：確保其他協程知道連線已死
            self.is_connected = False
            logger.info('ℹ️ 接收迴圈已結束，連線標記為斷開')
