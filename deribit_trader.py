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
import threading
import time
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from deribit_ws_client import DeribitWebSocket
from notifications import send_emergency_close_failed_notification, send_execution_failed_notification

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

        # P0-2: 下單前 WebSocket 健康預檢（防殭屍連線）
        # 可透過 Config.PRETRADE_PING_ENABLED 控制開關（預設開啟）。
        # 開啟時：預檢失敗會先嘗試強制重連，重連成功則繼續執行交易。
        # 關閉時：省略 ~50-150ms 延遲，改依賴心跳偵測（10s 週期）。
        if Config.PRETRADE_PING_ENABLED and not self.ws.ping_ws(timeout=1.0):
            logger.warning(
                "🛑 WS 健康預檢失敗（public/test 無回應）— 嘗試強制重連..."
            )
            if self._force_reconnect_ws(timeout=10.0):
                if self.ws.ping_ws(timeout=1.0):
                    logger.info("✅ 重連後預檢通過，繼續執行交易")
                else:
                    logger.error("❌ 重連後預檢仍失敗，放棄本次機會")
                    return {'success': False, 'failure_type': 'connection'}
            else:
                logger.error("❌ WS 強制重連失敗，放棄本次機會")
                return {'success': False, 'failure_type': 'connection'}

        # 重複開倉檢查已由 scan_orchestrator 在交易鎖內完成（pos_manager 記憶體檢查，微秒級）
        # 移除原本的 Deribit RPC 倉位預查（2x RPC ≈ 780ms），大幅縮短延遲

        perp_dir = 'buy' if strategy['perpDirection'] == 'long' else 'sell'
        # BTC-PERPETUAL 的 amount 單位是 USD（最小 10 USD，須為 10 的倍數）
        perp_amount_usd = round(amount * strategy['perpOpenPrice'] / 10) * 10
        legs = [
            {'name': strategy['callInstrument'], 'direction': strategy['callDirection'],
             'price': 0,                          'amount': amount,         'order_type': 'market'},
            {'name': strategy['putInstrument'],  'direction': strategy['putDirection'],
             'price': 0,                          'amount': amount,         'order_type': 'market'},
            {'name': 'BTC-PERPETUAL',            'direction': perp_dir,
             'price': 0,                          'amount': perp_amount_usd,'order_type': 'market'},
        ]

        # ── 步驟 1：三條腿併發下單 ────────────────────────────────────────────
        placed: List[Dict] = []
        no_response_legs: List[Dict] = []   # RPC timeout 的腿（status=timeout，不確定送達與否）
        rejected_legs:    List[Dict] = []   # 交易所明確拒絕的腿（status=rejected）
        leg_failed    = False
        has_rpc_timeout = False   # 只要有一腿 timeout 就算 rpc_timeout，需更嚴謹驗證

        def place_leg(leg: dict):
            result = self.ws.send_order(
                direction=leg['direction'],
                instrument=leg['name'],
                amount=leg['amount'],
                price=leg['price'],
                order_type=leg['order_type'],
            )
            return leg, result

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(place_leg, leg): leg for leg in legs}
            for fut in as_completed(futures):
                leg, result = fut.result()
                status = result.get('status')
                if status == 'ok' and 'order' in result:
                    order_id = result['order']['order_id']
                    logger.info(f"  ✅ 下單接受: {leg['name']} → order_id={order_id}")
                    placed.append({
                        'instrument': leg['name'],
                        'direction':  leg['direction'],
                        'order_id':   order_id,
                    })
                elif status == 'timeout':
                    logger.error(f"  ⏱️ 下單 RPC 無回應: {leg['name']}（不確定是否送達）")
                    leg_failed       = True
                    has_rpc_timeout  = True
                    no_response_legs.append({
                        'instrument': leg['name'],
                        'direction':  leg['direction'],
                    })
                else:
                    err = result.get('error', 'unknown')
                    logger.error(f"  ❌ 下單被交易所拒絕: {leg['name']} → {err}")
                    leg_failed = True
                    rejected_legs.append({
                        'instrument': leg['name'],
                        'direction':  leg['direction'],
                        'error':      str(err),
                    })

        if leg_failed:
            fail_reason = 'rpc_timeout' if has_rpc_timeout else 'exchange_rejected'
            logger.error(
                f"❌ 部分腿下單失敗 (reason={fail_reason})，撤銷已掛單並驗證實際倉位..."
            )
            self._cancel_orders(placed)
            # 查所有腿（含無回應腿）的實際倉位，避免遺漏已成交的 ghost order
            all_to_verify = placed + no_response_legs + rejected_legs
            actually_filled = self._verify_filled_with_retry(all_to_verify)
            if actually_filled:
                logger.warning(
                    f"🚨 發現 {len(actually_filled)} 條腿已成交，緊急平倉..."
                )
                self._emergency_close_legs(actually_filled)
            reject_errors = [f"{l['instrument']}: {l['error']}" for l in rejected_legs if l.get('error')]
            send_execution_failed_notification(strategy, fail_reason, reject_errors or None)
            return {'success': False, 'failure_type': fail_reason}

        # ── 步驟 2：等待三條腿全部成交 ────────────────────────────────────────
        logger.info(
            f"⏳ 等待三條腿成交（最多 {Config.ENTRY_FILL_TIMEOUT_SECONDS}s）..."
        )
        fill_states = self._wait_all_filled(placed, timeout=Config.ENTRY_FILL_TIMEOUT_SECONDS)

        unfilled = [o for o in placed if fill_states.get(o['order_id'], {}).get('state') != 'filled']
        filled   = [o for o in placed if fill_states.get(o['order_id'], {}).get('state') == 'filled']

        if unfilled:
            for o in unfilled:
                st = fill_states.get(o['order_id'], {}).get('state', '?')
                logger.error(f"  ❌ 未成交 ({st}): {o['instrument']}")
            self._cancel_orders(unfilled)
            if filled:
                logger.warning(f"🚨 緊急平倉 {len(filled)} 條已成交腿，避免裸倉...")
                self._emergency_close_legs(filled)
            send_execution_failed_notification(strategy, 'timeout')
            return {'success': False, 'failure_type': 'exchange'}

        logger.info("✅✅✅ 三條腿全部成交確認")
        for o in placed:
            o['avg_price'] = fill_states[o['order_id']].get('avg_price', 0.0)
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
            amount=amount,
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
                        results[order_id] = {
                            'state':     state,
                            'avg_price': data.get('average_price', 0.0),
                        }
                    return
                time.sleep(0.02)
            with lock:
                results[order_id] = {'state': 'timeout', 'avg_price': 0.0}

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
                results.setdefault(o['order_id'], {'state': 'timeout', 'avg_price': 0.0})

        return results

    def _verify_filled_with_retry(self, legs_to_verify: List[Dict]) -> List[Dict]:
        """
        驗證這些腿實際上是否有成交（防止 ghost order 造成裸腿）。
        策略：
          1) 先正常查 get_position
          2) 若任一腿回傳 None（RPC timeout），判定 WS 異常 → 強制重連 → 重試 1 次
          3) 若重試後仍有腿查不到，發出「請手動檢查」警報
        """
        def _check(leg: Dict):
            """回傳 ('filled'|'empty'|'unknown', leg)。"""
            pos = self.ws.get_position_ws(leg['instrument'])
            if pos is None:
                return ('unknown', leg)
            if abs(pos.get('size', 0)) > 0:
                return ('filled', leg)
            return ('empty', leg)

        # 第一次檢查
        results = [_check(l) for l in legs_to_verify]
        unknown = [l for s, l in results if s == 'unknown']

        if unknown:
            logger.error(
                f"⚠️ 有 {len(unknown)} 條腿的倉位查不到（RPC 無回應）— "
                f"強制 WS 重連後重試..."
            )
            if self._force_reconnect_ws(timeout=15.0):
                # 重試只查 unknown 腿
                retry_results = [_check(l) for l in unknown]
                # 合併結果（已知的保留）
                known_map = {id(l): s for s, l in results if s != 'unknown'}
                retry_map = {id(l): s for s, l in retry_results}
                results = [
                    (known_map.get(id(l), retry_map.get(id(l), 'unknown')), l)
                    for l in legs_to_verify
                ]
            else:
                logger.error("❌ 強制重連失敗，無法驗證倉位")

        filled = [l for s, l in results if s == 'filled']
        still_unknown = [l for s, l in results if s == 'unknown']

        if still_unknown:
            # 最後防線：至少發 Telegram 告知需手動檢查
            insts = ', '.join(l['instrument'] for l in still_unknown)
            logger.error(
                f"🚨 {len(still_unknown)} 條腿仍無法驗證倉位，"
                f"請立即登入 Deribit 手動檢查: {insts}"
            )
            try:
                send_emergency_close_failed_notification(
                    instrument=insts,
                    size=0,
                    direction='UNKNOWN — 驗證失敗，請手動核對 Deribit 實際倉位',
                )
            except Exception as e:
                logger.error(f"❌ 手動檢查通知發送失敗: {e}")

        return filled

    def _force_reconnect_ws(self, timeout: float = 15.0) -> bool:
        """
        強制 WebSocket 斷線 → 等待自動重連完成 → 驗證健康度。
        透過把 is_connected 設為 False 並排程 ws.close() 到 event loop，
        讓 _run_event_loop 走標準重連路徑。
        """
        import asyncio as _asyncio
        logger.warning("🔄 強制重連 WebSocket...")
        ws = self.ws
        try:
            ws.is_connected = False
            if ws.loop and ws.ws:
                _asyncio.run_coroutine_threadsafe(ws.ws.close(), ws.loop)
        except Exception as e:
            logger.error(f"❌ 強制關閉 WS 時發生例外: {e}")

        # 等待重連完成
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ws.is_connected and ws.is_authenticated and ws.ping_ws(timeout=1.0):
                logger.info("✅ WS 強制重連成功")
                return True
            time.sleep(0.3)
        logger.error(f"❌ WS 強制重連逾時（{timeout}s）")
        return False

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
        以市價緊急平倉已成交的腿，避免裸倉。
        使用 market order 確保立即成交，不受 tick size 限制。
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

            # 使用 market order：緊急平倉優先確保成交，不計滑點
            result = self.ws.send_order(
                direction=reverse_dir,
                instrument=inst,
                amount=actual_size,
                price=0,
                order_type='market',
            )
            if result.get('status') == 'ok' and 'order' in result:
                logger.info(
                    f"  ✅ 緊急平倉已送出: {inst} {reverse_dir} {actual_size}"
                )
            else:
                err = result.get('error', 'unknown')
                logger.error(f"  ❌❌ 緊急平倉失敗: {inst} → {err} — 需立即手動處理!")
                send_emergency_close_failed_notification(inst, actual_size, reverse_dir)
