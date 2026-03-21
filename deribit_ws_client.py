# arbitrage_bot/deribit_ws_client.py

"""
向後相容匯出層。
WebSocket 實作已拆分至：
  ws_core.py             → DeribitWebSocket（主類別）
  ws_rpc.py              → WsRpcMixin（RPC / 訂單操作）
  ws_subscription.py     → WsSubscriptionMixin（頻道訂閱）
  ws_message_handler.py  → WsMessageHandlerMixin（訊息路由）
"""

from ws_core import DeribitWebSocket

__all__ = ['DeribitWebSocket']
