# arbitrage_bot/ws_rpc.py

"""
WsRpcMixin：WebSocket RPC 呼叫與訂單操作。
提供同步阻塞的 RPC 介面，內部用 concurrent.futures.Future 橋接 asyncio。

Fix #4  斷線時將所有等待中的 RPC Future 標記為 ConnectionError
Fix #6  _next_id 加鎖，避免高頻下單時 ID 碰撞
"""

import asyncio
import json
import logging
import time
import threading
from concurrent.futures import Future
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class WsRpcMixin:
    """
    Mixin：提供 RPC 呼叫能力。
    依賴宿主類別 (DeribitWebSocket) 提供：
      self.loop, self.ws, self.is_connected, self.is_authenticated
    自行初始化：
      self._pending_requests, self._pending_lock, self._id_lock, self._request_id
    """

    def _init_rpc_state(self) -> None:
        self._pending_requests: Dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._id_lock      = threading.Lock()
        self._request_id   = 0

    # ── 公開訂單操作 ────────────────────────────────────────────────────────────

    def send_order(self, direction: str, instrument: str, amount: float,
                   price: float, order_type: str = 'limit',
                   post_only: bool = False) -> Optional[dict]:
        """送出單筆訂單（同步阻塞）。direction: 'buy' | 'sell'"""
        if not self.is_authenticated:
            logger.error('❌ WebSocket 尚未認證，無法下單')
            return None
        # 檢查 token 是否已過期
        if self._token_expires_at > 0 and time.time() > self._token_expires_at:
            logger.error('❌ Auth token 已過期，無法下單（等待自動刷新）')
            self.is_authenticated = False
            return None
        params: dict = {
            'instrument_name': instrument,
            'amount':          amount,
            'type':            order_type,
            'price':           price,
        }
        if post_only:
            params['post_only'] = True
        return self._rpc_call(f'private/{direction}', params, timeout=8)

    def cancel_order(self, order_id: str) -> Optional[dict]:
        """取消訂單（同步阻塞）。"""
        if not self.is_authenticated:
            return None
        return self._rpc_call('private/cancel', {'order_id': order_id}, timeout=8)

    def get_position_ws(self, instrument: str) -> Optional[dict]:
        """透過 WebSocket 查詢倉位。"""
        if not self.is_authenticated:
            return None
        return self._rpc_call('private/get_position',
                               {'instrument_name': instrument}, timeout=8)

    def get_order_state_ws(self, order_id: str) -> Optional[dict]:
        """查詢單筆訂單狀態。"""
        if not self.is_authenticated:
            return None
        return self._rpc_call('private/get_order_state', {'order_id': order_id}, timeout=3)

    def get_all_positions_ws(self) -> Optional[list]:
        """透過 WebSocket 查詢所有 BTC 持倉。"""
        if not self.is_authenticated:
            return None
        result = self._rpc_call('private/get_positions',
                                 {'currency': 'BTC', 'kind': 'any'}, timeout=8)
        return result if isinstance(result, list) else None

    def get_open_orders_ws(self, instrument: str) -> Optional[list]:
        """透過 WebSocket 查詢掛單。"""
        if not self.is_authenticated:
            return None
        result = self._rpc_call('private/get_open_orders_by_instrument',
                                 {'instrument_name': instrument}, timeout=8)
        return result if isinstance(result, list) else []

    # ── 內部工具 ────────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._id_lock:   # Fix #6
            self._request_id += 1
            return self._request_id

    def _flush_pending_requests(self) -> None:
        """Fix #4: 斷線時將所有 Future 標記為失敗，避免 thread 永久卡住。"""
        with self._pending_lock:
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.set_exception(ConnectionError('WebSocket disconnected'))
            self._pending_requests.clear()

    def _rpc_call(self, method: str, params: dict, timeout: float = 8) -> Optional[dict]:
        """同步阻塞的 WebSocket RPC 呼叫。"""
        if not self.loop or not self.is_connected:
            return None

        msg_id  = self._next_id()
        fut: Future = Future()

        with self._pending_lock:
            self._pending_requests[msg_id] = fut

        payload = json.dumps({
            'jsonrpc': '2.0',
            'id':      msg_id,
            'method':  method,
            'params':  params,
        })
        asyncio.run_coroutine_threadsafe(self.ws.send(payload), self.loop)

        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            logger.error(f'❌ RPC {method} 超時 ({timeout}s)，'
                         f'auth={self.is_authenticated}, conn={self.is_connected}')
            with self._pending_lock:
                self._pending_requests.pop(msg_id, None)
            return None
        except Exception as e:
            logger.error(f'❌ RPC {method} 失敗: {e}')
            with self._pending_lock:
                self._pending_requests.pop(msg_id, None)
            return None
