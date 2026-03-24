# arbitrage_bot/position_manager.py

"""
管理活躍的交易部位（多組並行），自動平倉。
三階段平倉邏輯已移至 closure_strategies.py。

支援每日最多 8 組並行部位，各自獨立追蹤與平倉。
"""

import time
import logging
import threading
from typing import Dict, List, Optional

from deribit_trader import DeribitTrader
from deribit_ws_client import DeribitWebSocket
from bot_state import bot_state
from state_store import load as load_state, save as save_state
from closure_strategies import manage_closure
from notifications import send_position_mismatch_notification

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, trader: DeribitTrader, ws_client: DeribitWebSocket):
        self.trader = trader
        self.ws     = ws_client
        self.active_positions: List[Dict] = []
        self.lock        = threading.Lock()
        self.is_running  = False
        self._thread     = threading.Thread(target=self._run, daemon=True)

        # 已成交的 Maker 平倉單 order_id 集合（由 WebSocket callback 更新）
        self._filled_maker_orders: set = set()

        # A2: 倉位核對計時器
        self._last_reconcile_time: float = 0.0

    # ── 生命週期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self.ws.subscribe_user_orders('BTC-PERPETUAL', self._on_order_update)

        # 讀取持久化部位（向後相容：若存的是單個 dict，包成 list）
        saved     = load_state()
        saved_pos = saved.get('active_positions') or saved.get('active_position')
        if saved_pos:
            if isinstance(saved_pos, dict):
                saved_pos = [saved_pos]
            logger.warning(
                f"📁 發現持久化部位，載入監控: {len(saved_pos)} 個"
            )
            with self.lock:
                self.active_positions = saved_pos
            bot_state.update_active_positions(saved_pos)

        self._thread.start()
        logger.info('✅ 部位管理器已啟動')

    def stop(self) -> None:
        self.is_running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info('🛑 部位管理器已停止')

    # ── 新增部位 ────────────────────────────────────────────────────────────────

    def add_position(self, expiry_timestamp: int, amount: float,
                     net_profit: float = 0.0, margin: float = 0.0,
                     strategy_name: str = '', strike: float = 0,
                     call_instrument: str = '', put_instrument: str = '',
                     perp_amount_usd: float = 0.0,
                     fill_call_price: float = 0.0, fill_put_price: float = 0.0,
                     fill_perp_price: float = 0.0,
                     call_direction: str = '', put_direction: str = '',
                     perp_direction: str = '',
                     gross_profit: float = 0.0, total_fees: float = 0.0,
                     funding_cost: float = 0.0, funding_direction: str = '') -> None:
        pos = {
            'instrument':        'BTC-PERPETUAL',
            'amount':            amount,
            'expiry_timestamp':  expiry_timestamp,
            'status':            'monitoring',
            'maker_order_id':    None,
            'entry_time':        time.time(),
            'net_profit_est':    net_profit,
            'margin_est':        margin,
            'strategy_name':     strategy_name,
            'strike':            strike,
            'call_instrument':   call_instrument,
            'put_instrument':    put_instrument,
            'perp_amount_usd':   perp_amount_usd,
            # 成交明細
            'fill_call_price':   fill_call_price,
            'fill_put_price':    fill_put_price,
            'fill_perp_price':   fill_perp_price,
            'call_direction':    call_direction,
            'put_direction':     put_direction,
            'perp_direction':    perp_direction,
            # 損益分析
            'gross_profit':      gross_profit,
            'total_fees':        total_fees,
            'funding_cost':      funding_cost,
            'funding_direction': funding_direction,
        }
        with self.lock:
            self.active_positions.append(pos.copy())
            positions_copy = list(self.active_positions)
        bot_state.update_active_positions(positions_copy)
        save_state('active_positions', positions_copy)
        logger.info(
            f"📈 新部位加入管理 [{call_instrument}]，"
            f"到期: {time.ctime(expiry_timestamp / 1000)}，"
            f"總計 {len(positions_copy)} 個"
        )

    def remove_position(self, call_instrument: str) -> None:
        """平倉完成後從列表移除，並同步持久化與 dashboard。"""
        with self.lock:
            self.active_positions = [
                p for p in self.active_positions
                if p.get('call_instrument') != call_instrument
            ]
            remaining = list(self.active_positions)
        bot_state.update_active_positions(remaining)
        save_state('active_positions', remaining)
        logger.info(f"📉 部位已移除: {call_instrument}，剩餘 {len(remaining)} 個")

    def update_position_fields(self, call_instrument: str, **updates) -> None:
        """更新指定部位的欄位（如 status、maker_order_id）。"""
        with self.lock:
            for p in self.active_positions:
                if p.get('call_instrument') == call_instrument:
                    p.update(updates)
                    break

    # ── WebSocket 訂單更新 callback ─────────────────────────────────────────────

    def _on_order_update(self, order: dict) -> None:
        order_id    = order.get('order_id')
        order_state = order.get('order_state')

        if order_state not in ('filled', 'cancelled'):
            return

        with self.lock:
            for pos in self.active_positions:
                if pos.get('maker_order_id') == order_id:
                    logger.info(f"✅ Maker 平倉單 {order_id} 狀態: {order_state}")
                    self._filled_maker_orders.add(order_id)
                    break

    # ── 主循環 ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self.is_running:
            with self.lock:
                positions = list(self.active_positions)

            if not positions:
                time.sleep(1)
                continue

            for pos in positions:
                try:
                    manage_closure(self, pos)
                except Exception as e:
                    logger.error(f"❌ 管理部位時發生錯誤 [{pos.get('call_instrument')}]: {e}", exc_info=True)

            # A2: 每 60 秒核對一次實際倉位
            now = time.time()
            if now - self._last_reconcile_time >= 60:
                self._last_reconcile_time = now
                try:
                    self._reconcile_positions()
                except Exception as e:
                    logger.error(f"❌ 倉位核對失敗: {e}", exc_info=True)

            time.sleep(1)

    # ── A2: 倉位核對 ─────────────────────────────────────────────────────────────

    def _reconcile_positions(self) -> None:
        """比對 bot 預期倉位與 Deribit 實際倉位。
        - 全部三腿消失 → 視為已手動平倉，自動移除，發通知
        - 部分腿消失   → 警報通知（裸倉異常）
        """
        with self.lock:
            positions = list(self.active_positions)
        if not positions:
            return

        actual = self.ws.get_all_positions_ws()
        if actual is None:
            logger.warning("⚠️ 無法取得 Deribit 實際倉位（可能 WS 未認證）")
            return

        actual_map = {p['instrument_name']: abs(p.get('size', 0)) for p in actual}

        for pos in positions:
            call_inst = pos.get('call_instrument', '')
            put_inst  = pos.get('put_instrument', '')

            # 只用選擇權兩腿判斷「全部關閉」。
            # Perp 由多組倉位共用，無法用 perp=0 判斷單一組是否關閉。
            option_legs   = [inst for inst in [call_inst, put_inst] if inst]
            missing_opts  = [inst for inst in option_legs if actual_map.get(inst, 0) == 0]

            if not missing_opts:
                logger.debug(f"✅ 倉位核對正常 [{call_inst}]")
                continue

            if len(missing_opts) == len(option_legs):
                # 兩腿選擇權都消失 → 視為手動平倉，自動移除
                logger.info(f"🧹 偵測到手動平倉 [{call_inst}]，自動移除 bot 部位")
                self.remove_position(call_inst)
            else:
                # 只有一腿消失 → 裸倉警報
                logger.warning(f"⚠️ 倉位核對異常 [{call_inst}]，以下合約實際倉位為零: {missing_opts}")
                send_position_mismatch_notification(pos, actual)
