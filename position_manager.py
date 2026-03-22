# arbitrage_bot/position_manager.py

"""
管理活躍的交易部位，自動平倉。
三階段平倉邏輯已移至 closure_strategies.py。

Fix #3  Taker 平倉前確認 Maker 單已取消，避免建立雙倉
Fix #5  所有部位狀態變化均同步更新 bot_state
Fix #10 部位狀態持久化至 state.json，重啟後自動恢復
"""

import time
import logging
import threading
from typing import Dict, Optional

from deribit_trader import DeribitTrader
from deribit_ws_client import DeribitWebSocket
from bot_state import bot_state
from state_store import load as load_state, save as save_state
from closure_strategies import manage_closure

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, trader: DeribitTrader, ws_client: DeribitWebSocket):
        self.trader = trader
        self.ws     = ws_client
        self.active_position: Optional[Dict] = None
        self.lock        = threading.Lock()
        self.is_running  = False
        self._thread     = threading.Thread(target=self._run, daemon=True)

        # Maker 平倉單成交事件（由 WebSocket callback 觸發）
        self._maker_order_filled = threading.Event()

    # ── 生命週期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self.ws.subscribe_user_orders('BTC-PERPETUAL', self._on_order_update)

        # Fix #10: 讀取持久化部位
        saved     = load_state()
        saved_pos = saved.get('active_position')
        if saved_pos:
            logger.warning(
                f"📁 發現持久化部位，載入監控: {saved_pos.get('status')} "
                f"到期={time.ctime(saved_pos['expiry_timestamp'] / 1000)}"
            )
            with self.lock:
                self.active_position = saved_pos
            bot_state.update_active_position(saved_pos)

        self._thread.start()
        logger.info('✅ 部位管理器已啟動')

    def stop(self) -> None:
        self.is_running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info('🛑 部位管理器已停止')

    # ── 新增部位 ────────────────────────────────────────────────────────────────

    def add_position(self, expiry_timestamp: int, amount: float,
                     net_profit: float = 0.0, margin: float = 0.0) -> None:
        pos = {
            'instrument':        'BTC-PERPETUAL',
            'amount':            amount,
            'expiry_timestamp':  expiry_timestamp,
            'status':            'monitoring',
            'maker_order_id':    None,
            'entry_time':        time.time(),
            'net_profit_est':    net_profit,
            'margin_est':        margin,
        }
        with self.lock:
            self.active_position = pos.copy()
        self._maker_order_filled.clear()
        bot_state.update_active_position(pos)
        save_state('active_position', pos)
        logger.info(f"📈 新部位加入管理，到期: {time.ctime(expiry_timestamp / 1000)}")

    # ── WebSocket 訂單更新 callback ─────────────────────────────────────────────

    def _on_order_update(self, order: dict) -> None:
        order_id    = order.get('order_id')
        order_state = order.get('order_state')

        with self.lock:
            pos = self.active_position
            if not pos:
                return
            if pos.get('maker_order_id') == order_id:
                if order_state in ('filled', 'cancelled'):
                    logger.info(f"✅ Maker 平倉單 {order_id} 狀態: {order_state}")
                    self._maker_order_filled.set()

    # ── 主循環 ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self.is_running:
            with self.lock:
                pos = self.active_position.copy() if self.active_position else None

            if not pos:
                time.sleep(1)
                continue

            try:
                manage_closure(self, pos)
            except Exception as e:
                logger.error(f"❌ 管理部位時發生錯誤: {e}", exc_info=True)

            time.sleep(1)
