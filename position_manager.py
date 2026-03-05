# arbitrage_bot/position_manager.py

"""
管理活躍的交易部位，自動平倉。
訂單狀態透過 WebSocket user.orders.BTC-PERPETUAL.raw 即時接收，
不再每秒輪詢 REST API。
"""

import time
import logging
import threading
from typing import Dict, Optional

from config import Config
from deribit_trader import DeribitTrader
from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, trader: DeribitTrader, ws_client: DeribitWebSocket):
        self.trader = trader
        self.ws = ws_client
        self.active_position: Optional[Dict] = None
        self.lock = threading.Lock()
        self.is_running = False
        self._thread = threading.Thread(target=self._run, daemon=True)

        # 追蹤 maker 平倉單的即時狀態（由 WebSocket callback 更新）
        self._maker_order_filled = threading.Event()

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        # 訂閱 BTC-PERPETUAL 的訂單狀態
        self.ws.subscribe_user_orders('BTC-PERPETUAL', self._on_order_update)
        self._thread.start()
        logger.info('✅ 部位管理器已啟動')

    def stop(self) -> None:
        self.is_running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info('🛑 部位管理器已停止')

    def add_position(self, expiry_timestamp: int, amount: float) -> None:
        with self.lock:
            self.active_position = {
                'instrument': 'BTC-PERPETUAL',
                'amount': amount,
                'expiry_timestamp': expiry_timestamp,
                'status': 'monitoring',
                'maker_order_id': None,
            }
        self._maker_order_filled.clear()
        logger.info(f"📈 新部位加入管理，到期: {time.ctime(expiry_timestamp / 1000)}")

    # ─── WebSocket 訂單更新 callback ──────────────────────────────────────────

    def _on_order_update(self, order: dict) -> None:
        """WebSocket user.orders 推送時呼叫（不再輪詢 REST）"""
        order_id = order.get('order_id')
        order_state = order.get('order_state')  # 'filled', 'cancelled', 'open', ...

        with self.lock:
            pos = self.active_position
            if not pos:
                return
            if pos.get('maker_order_id') == order_id:
                if order_state in ('filled', 'cancelled'):
                    logger.info(f"✅ Maker 平倉單 {order_id} 狀態: {order_state}")
                    self._maker_order_filled.set()

    # ─── 主循環 ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self.is_running:
            with self.lock:
                pos = self.active_position.copy() if self.active_position else None

            if not pos:
                time.sleep(1)
                continue

            try:
                self._manage_closure(pos)
            except Exception as e:
                logger.error(f"❌ 管理部位時發生錯誤: {e}", exc_info=True)

            time.sleep(1)

    def _manage_closure(self, position: Dict) -> None:
        now = int(time.time())
        expiry_sec = position['expiry_timestamp'] / 1000
        time_to_expiry = expiry_sec - now

        if time_to_expiry <= 0 and position['status'] != 'closed':
            logger.warning(f"⚠️ 部位已過期但未完成平倉，狀態: {position['status']}")
            with self.lock:
                self.active_position['status'] = 'closed'
            return

        # 階段一：進入平倉窗口，掛 Maker 單
        if (Config.POSITION_CLOSE_TRIGGER_SECONDS >= time_to_expiry > Config.TAKER_FORCE_CLOSE_SECONDS
                and position['status'] == 'monitoring'):
            logger.info(f"⏳ 進入平倉窗口（剩餘 {time_to_expiry:.0f}s），掛 Maker 平倉單...")
            self._try_close_maker(position)

        # 階段二：等待 Maker 單成交（由 WebSocket event 觸發，不輪詢）
        elif position['status'] == 'closing_maker':
            if self._maker_order_filled.wait(timeout=0):  # 非阻塞查詢
                logger.info("✅ Maker 平倉已完成")
                with self.lock:
                    self.active_position = None
                self._maker_order_filled.clear()

        # 階段三：強制 Taker 平倉
        elif (0 < time_to_expiry <= Config.TAKER_FORCE_CLOSE_SECONDS
              and position['status'] in ('monitoring', 'closing_maker')):
            logger.warning(f"🚨 強制 Taker 平倉（剩餘 {time_to_expiry:.0f}s）")
            self._force_close_taker(position)

    def _try_close_maker(self, position: Dict) -> None:
        perp_ticker = self.ws.get_ticker('BTC-PERPETUAL')
        if not perp_ticker:
            logger.warning("無法取得 BTC-PERPETUAL ticker，跳過本次 Maker 平倉嘗試")
            return

        current_pos = self.trader.get_position(position['instrument'])
        if not current_pos or current_pos.get('size', 0) == 0:
            logger.info("ℹ️ 部位已不存在，標記已關閉")
            with self.lock:
                self.active_position['status'] = 'closed'
            return

        price = (perp_ticker['best_ask_price'] if current_pos['size'] < 0
                 else perp_ticker['best_bid_price'])

        result = self.trader.close_position(
            instrument=position['instrument'],
            amount=abs(current_pos['size']),
            price=price,
            post_only=True,
        )

        if result and 'order' in result:
            order_id = result['order']['order_id']
            logger.info(f"✅ Maker 平倉單已掛出 order_id={order_id}")
            self._maker_order_filled.clear()
            with self.lock:
                self.active_position['status'] = 'closing_maker'
                self.active_position['maker_order_id'] = order_id
        else:
            logger.error(f"❌ Maker 平倉單失敗: {result}")

    def _force_close_taker(self, position: Dict) -> None:
        # 取消可能存在的 Maker 單
        if position['status'] == 'closing_maker' and position.get('maker_order_id'):
            self.trader.cancel(position['maker_order_id'])
            time.sleep(0.2)  # 短暫等待取消確認

        remaining_pos = self.trader.get_position(position['instrument'])
        if not remaining_pos or remaining_pos.get('size', 0) == 0:
            logger.info("ℹ️ Taker 平倉前部位已不存在")
            with self.lock:
                self.active_position = None
            return

        perp_ticker = self.ws.get_ticker('BTC-PERPETUAL')
        multiplier = 0.995 if remaining_pos['size'] < 0 else 1.005
        aggressive_price = perp_ticker['last_price'] * multiplier

        result = self.trader.close_position(
            instrument=position['instrument'],
            amount=abs(remaining_pos['size']),
            price=aggressive_price,
        )

        if result and 'order' in result:
            logger.info("✅✅ Taker 強制平倉單已送出")
        else:
            logger.error(f"❌❌ Taker 強制平倉失敗: {result}")

        with self.lock:
            self.active_position = None
