# arbitrage_bot/position_manager.py

"""
負責管理活躍的交易部位，特別是永續合約的自動平倉。
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
        self.ws_client = ws_client
        self.active_position: Optional[Dict] = None
        self.lock = threading.Lock()
        
        self.is_running = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        """啟動部位管理器線程"""
        if self.is_running:
            return
        self.is_running = True
        self.thread.start()
        logger.info('✅ 部位管理器已啟動')

    def stop(self):
        """停止部位管理器線程"""
        self.is_running = False
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        logger.info('🛑 部位管理器已停止')

    def add_position(self, expiry_timestamp: int, amount: float):
        """新增一個待管理的永續合約部位"""
        with self.lock:
            self.active_position = {
                'instrument': 'BTC-PERPETUAL',
                'amount': amount,
                'expiry_timestamp': expiry_timestamp,
                'status': 'monitoring'  # monitoring, closing_maker, closing_taker, closed
            }
        logger.info(f"📈 新部位已加入管理，將於 {time.ctime(expiry_timestamp/1000)} 到期")

    def _run(self):
        """背景執行緒的主循環"""
        while self.is_running:
            with self.lock:
                if not self.active_position:
                    time.sleep(1)
                    continue

                position = self.active_position.copy()
            
            try:
                self._manage_closure(position)
            except Exception as e:
                logger.error(f"❌ 管理部位時發生錯誤: {e}", exc_info=True)

            time.sleep(1) # 每秒檢查一次

    def _manage_closure(self, position: Dict):
        """管理單一部位的平倉邏輯"""
        now = int(time.time())
        expiry_sec = position['expiry_timestamp'] / 1000
        time_to_expiry = expiry_sec - now

        if time_to_expiry <= 0 and position['status'] != 'closed':
             logger.warning(f"⚠️ 部位已過期但未完成平倉，狀態: {position['status']}")
             # TODO: 在此處可以加入強制平倉邏輯
             with self.lock:
                self.active_position['status'] = 'closed'
             return
        
        # 階段一：觸發 Maker 平倉
        if Config.POSITION_CLOSE_TRIGGER_SECONDS >= time_to_expiry > Config.TAKER_FORCE_CLOSE_SECONDS and position['status'] == 'monitoring':
            logger.info(f"⏳ 進入平倉窗口（剩餘 {time_to_expiry:.0f}s），嘗試以 Maker 掛單平倉...")
            self._try_close_maker(position)
        
        # 階段二：檢查 Maker 訂單狀態
        elif position['status'] == 'closing_maker':
            self._check_maker_order(position)

        # 階段三：強制 Taker 平倉
        elif 0 < time_to_expiry <= Config.TAKER_FORCE_CLOSE_SECONDS and position['status'] in ['monitoring', 'closing_maker']:
            logger.warning(f"🚨 進入強制平倉時間（剩餘 {time_to_expiry:.0f}s），取消舊訂單並以 Taker 平倉！")
            self._force_close_taker(position)

    def _try_close_maker(self, position: Dict):
        perp_ticker = self.ws_client.get_ticker('BTC-PERPETUAL')
        if not perp_ticker:
            logger.warning("無法獲取永續合約 ticker，無法掛出 Maker 單")
            return
        
        current_pos = self.trader.get_position(position['instrument'])
        if not current_pos or current_pos.get('size', 0) == 0:
            logger.info("ℹ️ Maker平倉前檢查：部位已不存在，標記為已關閉。")
            with self.lock:
                self.active_position['status'] = 'closed'
            return

        # 根據部位方向決定掛單價格（掛在對手盤一檔）
        price = perp_ticker['best_ask_price'] if current_pos['size'] < 0 else perp_ticker['best_bid_price']
        
        result = self.trader.close_position(
            instrument=position['instrument'],
            amount=abs(current_pos['size']),
            order_type='limit',
            price=price,
            post_only=True
        )
        
        if 'order' in result:
            order_id = result['order']['order_id']
            logger.info(f"✅ Maker 平倉單已成功掛出，訂單ID: {order_id}")
            with self.lock:
                self.active_position['status'] = 'closing_maker'
                self.active_position['maker_order_id'] = order_id
        else:
            logger.error(f"❌ Maker 平倉單失敗: {result.get('error')}")

    def _check_maker_order(self, position: Dict):
        order_id = position.get('maker_order_id')
        if not order_id: return

        open_orders = self.trader.get_open_orders_by_instrument(position['instrument'])
        if not any(o['order_id'] == order_id for o in open_orders):
            logger.info(f"✅ Maker 平倉單 ({order_id}) 已成交或被取消，部位已平倉！")
            with self.lock:
                self.active_position = None # 平倉成功，清除部位
            # TODO: 可以發送一個平倉成功的通知
            return
        logger.debug(f"Maker 平倉單 ({order_id}) 仍在掛單中...")

    def _force_close_taker(self, position: Dict):
        # 1. 取消可能存在的 Maker 訂單
        if position['status'] == 'closing_maker' and 'maker_order_id' in position:
            self.trader.cancel(position['maker_order_id'])
            time.sleep(0.5) # 等待取消確認

        # 2. 獲取剩餘部位大小
        remaining_pos = self.trader.get_position(position['instrument'])
        if not remaining_pos or remaining_pos.get('size', 0) == 0:
            logger.info("ℹ️ Taker 強制平倉前檢查：部位已不存在。")
            with self.lock:
                self.active_position = None
            return

        # 3. 以市價（或積極限價）平掉剩餘部位
        logger.info(f"準備以 Taker 方式平掉剩餘 {remaining_pos['size']} 部位")
        # 為避免滑價，可以使用一個寬鬆的限價單來代替市價單
        perp_ticker = self.ws_client.get_ticker('BTC-PERPETUAL')
        price_multiplier = 0.995 if remaining_pos['size'] < 0 else 1.005 # 買單價格高5%，賣單價格低5%
        aggressive_price = perp_ticker['last_price'] * price_multiplier
        
        result = self.trader.close_position(
            instrument=position['instrument'],
            amount=abs(remaining_pos['size']),
            order_type='limit', # 使用限價單模擬市價
            price=aggressive_price
        )

        if 'order' in result:
            logger.info("✅✅ Taker 強制平倉單已送出！")
            with self.lock:
                self.active_position = None # 不論是否成交，都假定任務完成並清除
        else:
            logger.error(f"❌❌ Taker 強制平倉失敗: {result.get('error')}")