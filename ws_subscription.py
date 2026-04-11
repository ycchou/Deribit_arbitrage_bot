# arbitrage_bot/ws_subscription.py

"""
WsSubscriptionMixin：管理 WebSocket 頻道訂閱。
支援公開 ticker 頻道與私有訂單頻道，含重連後自動重訂閱邏輯。

Fix #2  重連後重新訂閱所有頻道
Fix #7  pending_subscriptions 存取全面加鎖
"""

import asyncio
import json
import logging
import threading
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)


class WsSubscriptionMixin:
    """
    Mixin：提供頻道訂閱能力。
    依賴宿主類別 (DeribitWebSocket) 提供：
      self.loop, self.ws, self.is_connected, self.is_authenticated, self._next_id()
    自行初始化：
      self._sub_lock, self.subscribed_instruments, self.pending_subscriptions,
      self._user_order_callbacks
    """

    def _init_subscription_state(self) -> None:
        self._sub_lock               = threading.Lock()   # Fix #7
        self.subscribed_instruments: set     = set()
        self.pending_subscriptions: set      = set()
        self._user_order_callbacks: Dict[str, Callable] = {}

    # ── 公開訂閱介面 ────────────────────────────────────────────────────────────

    def subscribe_instruments(self, instruments: List[str]) -> bool:
        """訂閱合約 ticker（smart：只訂閱尚未訂閱的）。"""
        with self._sub_lock:
            new_instruments = [i for i in instruments
                               if i not in self.subscribed_instruments]
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
                    with self._sub_lock:
                        self.subscribed_instruments.update(new_instruments)
                return result
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                logger.error(f'❌ 訂閱失敗: {err_msg}，放入 pending 等待重試')
                future.cancel()
                with self._sub_lock:
                    self.pending_subscriptions.update(channels)
                return False
        else:
            with self._sub_lock:
                self.pending_subscriptions.update(channels)
            return False

    def subscribe_user_orders(self, instrument: str,
                               callback: Callable[[dict], None]) -> None:
        """訂閱私有訂單更新頻道 user.orders.{instrument}.raw。"""
        self._user_order_callbacks[instrument] = callback
        channel = f'user.orders.{instrument}.raw'
        if self.is_connected and self.loop:
            asyncio.run_coroutine_threadsafe(
                self._subscribe_channels([channel]), self.loop
            )
        else:
            with self._sub_lock:
                self.pending_subscriptions.add(channel)

    # ── 重連時恢復訂閱 ──────────────────────────────────────────────────────────

    def _rebuild_pending_on_reconnect(self) -> None:
        """Fix #2: 每次重連前，將已訂閱頻道移回 pending 以確保重訂閱。"""
        with self._sub_lock:
            for inst in self.subscribed_instruments:
                self.pending_subscriptions.add(f'ticker.{inst}.raw')
            self.subscribed_instruments.clear()
            for inst in self._user_order_callbacks:
                self.pending_subscriptions.add(f'user.orders.{inst}.raw')

        # 清除 ticker 更新時間，使 is_data_ready 拒絕過時數據
        with self.data_lock:
            self.last_update_time.clear()
            logger.info(f'🔄 重連：已清除 {len(self.ticker_data)} 個合約的更新時間戳')

    async def _flush_pending_subscriptions(self) -> None:
        """連線後將 pending 頻道全部訂閱。"""
        with self._sub_lock:
            pending = list(self.pending_subscriptions)
        if not pending:
            return

        success = await self._subscribe_channels(pending)
        if success:
            with self._sub_lock:
                for ch in pending:
                    self.pending_subscriptions.discard(ch)
                    if ch.startswith('ticker.') and ch.endswith('.raw'):
                        inst = ch[len('ticker.'):-len('.raw')]
                        self.subscribed_instruments.add(inst)

    # ── 底層訂閱 ────────────────────────────────────────────────────────────────

    async def _subscribe_channels(self, channels: List[str]) -> bool:
        if not self.ws or not self.is_connected:
            with self._sub_lock:
                self.pending_subscriptions.update(channels)
            return False
        try:
            public  = [c for c in channels if not c.startswith('user.')]
            private = [c for c in channels if     c.startswith('user.')]

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
                with self._sub_lock:
                    self.pending_subscriptions.update(private)
                logger.warning('⚠️ 私有頻道訂閱延遲：等待認證')
            return True
        except Exception as e:
            logger.error(f'❌ 訂閱失敗: {e}')
            return False
