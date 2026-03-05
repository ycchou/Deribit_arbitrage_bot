# arbitrage_bot/deribit_trader.py

"""
處理所有交易執行邏輯。
下單、平倉、查詢倉位全部走 WebSocket 私有 API（低延遲）。
"""

import logging
from typing import Dict, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)


class DeribitTrader:
    def __init__(self, ws_client: DeribitWebSocket):
        self.ws = ws_client

    def execute_arbitrage_strategy(self, strategy: Dict, amount: float) -> Optional[Dict]:
        """
        併發送出三條腿套利訂單（全部走 WebSocket 私有 API）。
        任一條腿失敗時，自動撤銷已成功的訂單（補償邏輯）。
        """
        logger.info(f"🚀 執行策略: {strategy['strategyName']} @ ${strategy['strike']} ({amount} BTC)")

        perp_dir = 'buy' if strategy['perpDirection'] == 'long' else 'sell'

        legs = [
            {'name': strategy['callInstrument'], 'direction': strategy['callDirection'],
             'price': strategy['callPrice']},
            {'name': strategy['putInstrument'],  'direction': strategy['putDirection'],
             'price': strategy['putPrice']},
            {'name': 'BTC-PERPETUAL',            'direction': perp_dir,
             'price': strategy['perpOpenPrice']},
        ]

        # 併發送出三條腿
        placed: List[Dict] = []   # 已成功的訂單 {instrument, order_id}
        failed = False

        def place_leg(leg: dict):
            result = self.ws.send_order(
                direction=leg['direction'],
                instrument=leg['name'],
                amount=amount,
                price=leg['price'],
            )
            return leg['name'], result

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(place_leg, leg): leg for leg in legs}
            for fut in as_completed(futures):
                instrument, result = fut.result()
                if result and 'order' in result:
                    order_id = result['order']['order_id']
                    logger.info(f"  ✅ 下單成功: {instrument} → order_id={order_id}")
                    placed.append({'instrument': instrument, 'order_id': order_id})
                else:
                    err = result.get('message') if result else '無回應'
                    logger.error(f"  ❌ 下單失敗: {instrument} → {err}")
                    failed = True

        if not failed:
            logger.info("✅✅✅ 三條腿全部下單成功")
            return {'success': True, 'orders': placed}

        # ── 補償：撤銷已成功的訂單 ────────────────────────────────────────────
        logger.error("❌ 部分條腿失敗，執行補償撤單...")
        for order in placed:
            cancel_result = self.ws.cancel_order(order['order_id'])
            if cancel_result:
                logger.info(f"  🔄 已撤銷: {order['instrument']} order_id={order['order_id']}")
            else:
                logger.error(f"  ❌ 撤銷失敗，請手動處理: {order['instrument']} order_id={order['order_id']}")
        return None

    def close_position(self, instrument: str, amount: float,
                       order_type: str = 'limit', price: Optional[float] = None,
                       post_only: bool = False) -> Dict:
        """平倉指定合約"""
        position = self.get_position(instrument)
        if not position or position.get('size', 0) == 0:
            logger.info(f"ℹ️ {instrument} 無需平倉，當前無部位。")
            return {'message': 'No position to close.'}

        direction = 'buy' if position['size'] < 0 else 'sell'

        if order_type == 'limit' and price is None:
            raise ValueError("限價平倉必須提供價格")

        result = self.ws.send_order(
            direction=direction,
            instrument=instrument,
            amount=abs(position['size']),
            price=price or 0,
            order_type=order_type,
            post_only=post_only,
        )
        return result or {}

    def get_position(self, instrument: str) -> Dict:
        return self.ws.get_position_ws(instrument) or {}

    def get_open_orders_by_instrument(self, instrument: str) -> List[Dict]:
        return self.ws.get_open_orders_ws(instrument) or []

    def cancel(self, order_id: str) -> Dict:
        logger.info(f"正在取消訂單: {order_id}")
        return self.ws.cancel_order(order_id) or {}
