# arbitrage_bot/deribit_trader.py

"""
處理所有交易執行邏輯。
下單、平倉、查詢倉位全部走 WebSocket 私有 API（低延遲）。

開倉流程（execute_arbitrage_strategy）：
  步驟 1：三條腿併發下單
  步驟 2：輪詢等待全部成交確認（最多 ENTRY_FILL_TIMEOUT_SECONDS）
  步驟 3：若任一條腿超時或被拒絕：
            - 撤銷未成交的單
            - 緊急平倉已成交的腿（避免裸倉）
"""

import logging
import math
import threading
import time
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from deribit_ws_client import DeribitWebSocket
from notifications import send_emergency_close_failed_notification

logger = logging.getLogger(__name__)


class DeribitTrader:
    def __init__(self, ws_client: DeribitWebSocket):
        self.ws = ws_client

    # ── 主要交易入口 ─────────────────────────────────────────────────────────────

    def execute_arbitrage_strategy(self, strategy: Dict, amount: float) -> Optional[Dict]:
        """
        三條腿套利執行：下單 → 等待成交確認 → 異常處理。
        回傳 {'success': True, 'orders': [...]} 或 None。
        """
        logger.info(
            f"🚀 執行策略: {strategy['strategyName']} @ ${strategy['strike']} ({amount} BTC)"
        )

        perp_dir = 'buy' if strategy['perpDirection'] == 'long' else 'sell'
        # BTC-PERPETUAL 的 amount 單位是 USD（最小 10 USD，須為 10 的倍數）
        perp_amount_usd = round(amount * strategy['perpOpenPrice'] / 10) * 10
        legs = [
            {'name': strategy['callInstrument'], 'direction': strategy['callDirection'],
             'price': strategy['callPrice'],      'amount': amount},
            {'name': strategy['putInstrument'],  'direction': strategy['putDirection'],
             'price': strategy['putPrice'],       'amount': amount},
            {'name': 'BTC-PERPETUAL',            'direction': perp_dir,
             'price': strategy['perpOpenPrice'],  'amount': perp_amount_usd},
        ]

        # ── 步驟 1：三條腿併發下單 ────────────────────────────────────────────
        placed: List[Dict] = []
        api_failed = False

        def place_leg(leg: dict):
            result = self.ws.send_order(
                direction=leg['direction'],
                instrument=leg['name'],
                amount=leg['amount'],
                price=leg['price'],
            )
            return leg, result

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(place_leg, leg): leg for leg in legs}
            for fut in as_completed(futures):
                leg, result = fut.result()
                if result and 'order' in result:
                    order_id = result['order']['order_id']
                    logger.info(f"  ✅ 下單接受: {leg['name']} → order_id={order_id}")
                    placed.append({
                        'instrument': leg['name'],
                        'direction':  leg['direction'],
                        'order_id':   order_id,
                    })
                else:
                    err = result.get('message') if result else '無回應'
                    logger.error(f"  ❌ 下單被 API 拒絕: {leg['name']} → {err}")
                    api_failed = True

        if api_failed:
            logger.error("❌ 部分條腿被 API 拒絕，撤銷已掛單...")
            self._cancel_orders(placed)
            # 撤單後確認實際持倉，已成交的腿需緊急平倉（避免裸倉）
            actually_filled = [
                o for o in placed
                if abs((self.get_position(o['instrument']) or {}).get('size', 0)) > 0
            ]
            if actually_filled:
                logger.warning(f"🚨 發現 {len(actually_filled)} 條腿已成交，緊急平倉...")
                self._emergency_close_legs(actually_filled)
            return None

        # ── 步驟 2：等待三條腿全部成交 ────────────────────────────────────────
        logger.info(
            f"⏳ 等待三條腿成交（最多 {Config.ENTRY_FILL_TIMEOUT_SECONDS}s）..."
        )
        fill_states = self._wait_all_filled(placed, timeout=Config.ENTRY_FILL_TIMEOUT_SECONDS)

        unfilled = [o for o in placed if fill_states.get(o['order_id']) != 'filled']
        filled   = [o for o in placed if fill_states.get(o['order_id']) == 'filled']

        if unfilled:
            for o in unfilled:
                st = fill_states.get(o['order_id'], '?')
                logger.error(f"  ❌ 未成交 ({st}): {o['instrument']}")
            self._cancel_orders(unfilled)
            if filled:
                logger.warning(f"🚨 緊急平倉 {len(filled)} 條已成交腿，避免裸倉...")
                self._emergency_close_legs(filled)
            return None

        logger.info("✅✅✅ 三條腿全部成交確認")
        return {'success': True, 'orders': placed}

    # ── 平倉 ────────────────────────────────────────────────────────────────────

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

    # ── 查詢 ────────────────────────────────────────────────────────────────────

    def get_position(self, instrument: str) -> Dict:
        return self.ws.get_position_ws(instrument) or {}

    def get_order_state(self, order_id: str) -> Dict:
        return self.ws.get_order_state_ws(order_id) or {}

    def get_open_orders_by_instrument(self, instrument: str) -> List[Dict]:
        return self.ws.get_open_orders_ws(instrument) or []

    def cancel(self, order_id: str) -> Dict:
        logger.info(f"正在取消訂單: {order_id}")
        return self.ws.cancel_order(order_id) or {}

    # ── 內部工具 ────────────────────────────────────────────────────────────────

    def _wait_all_filled(self, placed: List[Dict], timeout: float) -> Dict[str, str]:
        """
        三條腿併發輪詢，直到全部成交或超時。
        回傳 {order_id: state}，state: 'filled' | 'cancelled' | 'rejected' | 'timeout'
        """
        results: Dict[str, str] = {}
        lock     = threading.Lock()
        deadline = time.time() + timeout

        def poll_one(order_id: str) -> None:
            while time.time() < deadline:
                data  = self.get_order_state(order_id)
                state = data.get('order_state', '')
                if state in ('filled', 'cancelled', 'rejected'):
                    with lock:
                        results[order_id] = state
                    return
                time.sleep(0.15)
            with lock:
                results[order_id] = 'timeout'

        threads = [
            threading.Thread(target=poll_one, args=(o['order_id'],), daemon=True)
            for o in placed
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout + 2)

        with lock:
            for o in placed:
                results.setdefault(o['order_id'], 'timeout')

        return results

    def _cancel_orders(self, orders: List[Dict]) -> None:
        """撤銷訂單列表。"""
        for o in orders:
            result = self.ws.cancel_order(o['order_id'])
            if result:
                logger.info(f"  🔄 已撤銷: {o['instrument']} order_id={o['order_id']}")
            else:
                logger.error(
                    f"  ❌ 撤銷失敗，請手動處理: {o['instrument']} order_id={o['order_id']}"
                )

    def _emergency_close_legs(self, filled_legs: List[Dict]) -> None:
        """
        以積極限價緊急平倉已成交的腿，避免裸倉。
        查詢實際倉位大小以正確處理部分成交的情況。
        """
        for leg in filled_legs:
            inst = leg['instrument']

            # 查詢實際持倉大小（處理部分成交）
            position    = self.get_position(inst)
            actual_size = abs(position.get('size', 0)) if position else 0
            if actual_size == 0:
                logger.info(f"  ℹ️ {inst} 部位為零，無需緊急平倉")
                continue

            reverse_dir = 'sell' if leg['direction'] == 'buy' else 'buy'
            ticker      = self.ws.get_ticker(inst)
            if not ticker:
                logger.error(f"  🚨 無法取得 {inst} ticker，需立即手動平倉!")
                continue

            # 稍微穿越 spread 確保成交，並對齊 tick size
            # BTC-PERPETUAL tick=$0.5，期權 tick=$0.0001
            tick = 0.5 if inst == 'BTC-PERPETUAL' else 0.0001
            if reverse_dir == 'buy':
                raw = ticker['best_ask_price'] * 1.005
                price = math.ceil(raw / tick) * tick
            else:
                raw = ticker['best_bid_price'] * 0.995
                price = math.floor(raw / tick) * tick

            result = self.ws.send_order(
                direction=reverse_dir,
                instrument=inst,
                amount=actual_size,
                price=price,
            )
            if result and 'order' in result:
                logger.info(
                    f"  ✅ 緊急平倉已送出: {inst} {reverse_dir} {actual_size}"
                )
            else:
                logger.error(f"  ❌❌ 緊急平倉失敗: {inst} — 需立即手動處理!")
                send_emergency_close_failed_notification(inst, actual_size, reverse_dir)
