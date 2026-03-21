# arbitrage_bot/ws_message_handler.py

"""
WsMessageHandlerMixin：WebSocket 訊息路由與分發。
將 _handle_message 拆成三個職責清晰的子方法。
"""

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class WsMessageHandlerMixin:
    """
    Mixin：處理收到的 WebSocket 訊息。
    依賴宿主類別 (DeribitWebSocket) 提供：
      self.data_lock, self.ticker_data, self.last_update_time,
      self._on_ticker_update, self._pending_requests, self._pending_lock,
      self._user_order_callbacks
    並呼叫 self.is_authenticated（寫入）。
    """

    async def _handle_message(self, data: dict) -> None:
        """訊息路由入口：依訊息型態分派至對應處理方法。"""
        if 'id' in data:
            await self._handle_rpc_response(data)
            return
        if 'params' in data:
            channel = data['params'].get('channel', '')
            payload = data['params'].get('data')
            if channel.startswith('ticker.'):
                self._handle_ticker_update(channel, payload)
            elif channel.startswith('user.orders.'):
                self._handle_order_update(channel, payload)

    async def _handle_rpc_response(self, data: dict) -> None:
        """處理 RPC 回應（含認證）。"""
        msg_id = data['id']

        # 認證回應
        if 'result' in data and isinstance(data['result'], dict):
            if 'access_token' in data['result']:
                self.is_authenticated = True
                return

        # 一般 RPC Future
        with self._pending_lock:
            fut = self._pending_requests.pop(msg_id, None)
        if fut and not fut.done():
            if 'error' in data:
                fut.set_exception(RuntimeError(str(data['error'])))
            else:
                fut.set_result(data.get('result'))

    def _handle_ticker_update(self, channel: str, payload: Optional[dict]) -> None:
        """更新 ticker 快取並觸發事件 callback。"""
        if not payload:
            return
        instrument = payload.get('instrument_name')
        if not instrument:
            return

        with self.data_lock:
            is_new = instrument not in self.ticker_data
            self.ticker_data[instrument]      = payload
            self.last_update_time[instrument] = time.time()

        if is_new:
            logger.info(f'📊 首次接收 {instrument} 數據')

        if self._on_ticker_update:
            try:
                self._on_ticker_update(instrument)
            except Exception as e:
                logger.error(f'❌ on_ticker_update callback 錯誤: {e}')

    def _handle_order_update(self, channel: str, payload) -> None:
        """分發私有訂單更新至對應合約的 callback。"""
        callbacks = self._user_order_callbacks
        if not isinstance(payload, list):
            return
        for order in payload:
            inst = order.get('instrument_name', '')
            cb   = callbacks.get(inst)
            if cb:
                try:
                    cb(order)
                except Exception as e:
                    logger.error(f'❌ user_order callback 錯誤: {e}')
