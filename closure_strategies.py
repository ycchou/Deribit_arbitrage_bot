# arbitrage_bot/closure_strategies.py

"""
平倉策略（Maker 優先 + 市價兜底）：
  1. 到期前 60s  → 掛 Maker（post-only）限價單
  2. 每 5s 未成交 → 取消舊單，以最新 best price 重掛
  3. 到期前 10s 仍未成交 → 取消 Maker，改市價單沖銷
  4. 已過期（bot 錯過窗口）→ 緊急市價平倉

所有函式接收 PositionManager 實例，透過其屬性存取共用狀態，
避免傳遞大量參數。
"""

import time
import logging
from typing import TYPE_CHECKING

from config import Config
from bot_state import bot_state
from notifications import send_position_closed_notification

if TYPE_CHECKING:
    from position_manager import PositionManager

logger = logging.getLogger(__name__)

MAKER_REFRESH_INTERVAL = 5  # 每 5 秒刷新 Maker 掛單價格


def manage_closure(manager: 'PositionManager', position: dict) -> None:
    """
    根據剩餘到期時間決定執行哪個平倉階段。
    由 PositionManager._run() 每秒呼叫一次。
    """
    call_inst      = position.get('call_instrument', '')
    now            = int(time.time())
    expiry_sec     = position['expiry_timestamp'] / 1000
    time_to_expiry = expiry_sec - now

    # ── 已過期：緊急市價平倉 ──────────────────────────────────────────
    if time_to_expiry <= 0 and position['status'] != 'closed':
        _emergency_market_close(manager, position)
        return

    # ── 階段一：進入平倉窗口（60s），首次掛 Maker 單 ─────────────────
    if (Config.POSITION_CLOSE_TRIGGER_SECONDS >= time_to_expiry > Config.TAKER_FORCE_CLOSE_SECONDS
            and position['status'] == 'monitoring'):
        logger.info(f"⏳ 進入平倉窗口 [{call_inst}]（剩餘 {time_to_expiry:.0f}s），掛 Maker 平倉單...")
        _try_close_maker(manager, position)

    # ── 階段二：closing_maker → 檢查成交 / 5s 刷新 / 10s 強制市價 ──
    elif position['status'] == 'closing_maker':
        # 2a. 檢查 WebSocket 回報的成交
        maker_oid = position.get('maker_order_id')
        if maker_oid:
            with manager.lock:
                is_filled = maker_oid in manager._filled_maker_orders
                if is_filled:
                    manager._filled_maker_orders.discard(maker_oid)
            if is_filled:
                logger.info(f"✅ Maker 平倉已成交 [{call_inst}]")
                send_position_closed_notification(position, 'maker')
                manager.remove_position(call_inst)
                return

        # 2b. 到期前 <=10s 仍未成交 → 取消 Maker，強制市價
        if time_to_expiry <= Config.TAKER_FORCE_CLOSE_SECONDS:
            logger.warning(
                f"🚨 Maker 未成交，強制市價平倉 [{call_inst}]（剩餘 {time_to_expiry:.0f}s）"
            )
            _force_close_market(manager, position)
            return

        # 2c. 每 MAKER_REFRESH_INTERVAL 秒刷新 Maker 掛單價格
        maker_time = position.get('maker_order_time', 0)
        if now - maker_time >= MAKER_REFRESH_INTERVAL:
            _refresh_maker_order(manager, position)

    # ── 階段三 fallback：monitoring 且 <=10s（Stage 1 從未觸發的邊界情況）──
    elif (0 < time_to_expiry <= Config.TAKER_FORCE_CLOSE_SECONDS
          and position['status'] == 'monitoring'):
        logger.warning(f"🚨 直接強制市價平倉 [{call_inst}]（剩餘 {time_to_expiry:.0f}s）")
        _force_close_market(manager, position)


# ── Helper functions ─────────────────────────────────────────────────────────


def _try_close_maker(manager: 'PositionManager', position: dict) -> None:
    """掛出 post-only Maker 平倉單。"""
    call_inst   = position.get('call_instrument', '')
    perp_ticker = manager.ws.get_ticker('BTC-PERPETUAL')
    if not perp_ticker:
        logger.warning(f"無法取得 BTC-PERPETUAL ticker [{call_inst}]，跳過本次 Maker 平倉嘗試")
        return

    current_pos = manager.trader.get_position(position['instrument'])
    if not current_pos or current_pos.get('size', 0) == 0:
        logger.info(f"ℹ️ 部位已不存在 [{call_inst}]，標記已關閉")
        manager.remove_position(call_inst)
        return

    price = (perp_ticker['best_ask_price'] if current_pos['size'] < 0
             else perp_ticker['best_bid_price'])

    close_amount = position.get('perp_amount_usd') or abs(current_pos['size'])

    result = manager.trader.close_position(
        instrument=position['instrument'],
        amount=close_amount,
        price=price,
        post_only=True,
    )

    if result and 'order' in result:
        order_id = result['order']['order_id']
        logger.info(f"✅ Maker 平倉單已掛出 [{call_inst}] order_id={order_id} @ ${price:,.0f}")
        manager.update_position_fields(
            call_inst,
            status='closing_maker',
            maker_order_id=order_id,
            maker_order_time=int(time.time()),
            close_perp_price=price,
        )
    else:
        logger.error(f"❌ Maker 平倉單失敗 [{call_inst}]: {result}")


def _refresh_maker_order(manager: 'PositionManager', position: dict) -> None:
    """取消舊 Maker 單，以最新 best price 重新掛出。"""
    call_inst = position.get('call_instrument', '')
    old_oid   = position.get('maker_order_id')

    if old_oid:
        manager.trader.cancel(old_oid)

    perp_ticker = manager.ws.get_ticker('BTC-PERPETUAL')
    if not perp_ticker:
        logger.warning(f"無法取得 ticker [{call_inst}]，等下次刷新")
        return

    current_pos = manager.trader.get_position(position['instrument'])
    if not current_pos or current_pos.get('size', 0) == 0:
        logger.info(f"ℹ️ 部位已不存在 [{call_inst}]，標記已關閉")
        manager.remove_position(call_inst)
        return

    price = (perp_ticker['best_ask_price'] if current_pos['size'] < 0
             else perp_ticker['best_bid_price'])

    close_amount = position.get('perp_amount_usd') or abs(current_pos['size'])

    result = manager.trader.close_position(
        instrument=position['instrument'],
        amount=close_amount,
        price=price,
        post_only=True,
    )

    if result and 'order' in result:
        order_id = result['order']['order_id']
        logger.info(f"🔄 Maker 掛單已刷新 [{call_inst}] order_id={order_id} @ ${price:,.0f}")
        manager.update_position_fields(
            call_inst,
            maker_order_id=order_id,
            maker_order_time=int(time.time()),
            close_perp_price=price,
        )
    else:
        logger.error(f"❌ Maker 刷新失敗 [{call_inst}]: {result}")


def _force_close_market(manager: 'PositionManager', position: dict) -> None:
    """取消殘留 Maker 單，強制市價平倉。"""
    call_inst = position.get('call_instrument', '')

    # 取消殘留 Maker 單
    if position.get('maker_order_id'):
        cancel_result = manager.trader.cancel(position['maker_order_id'])
        if not cancel_result:
            logger.warning(f"⚠️ Maker 單取消指令無回應 [{call_inst}]，查詢確認...")
        time.sleep(0.5)

        open_orders = manager.trader.get_open_orders_by_instrument(position['instrument'])
        if any(o.get('order_id') == position['maker_order_id'] for o in open_orders):
            logger.error(
                f"❌ Maker 單 {position['maker_order_id']} 仍在掛單中 [{call_inst}]，"
                "中止市價平倉以避免重複建倉"
            )
            return

    remaining_pos = manager.trader.get_position(position['instrument'])
    if not remaining_pos or remaining_pos.get('size', 0) == 0:
        logger.info(f"ℹ️ 市價平倉前部位已不存在 [{call_inst}]")
        manager.remove_position(call_inst)
        return

    perp_ticker  = manager.ws.get_ticker('BTC-PERPETUAL')
    market_price = perp_ticker['last_price'] if perp_ticker else None

    close_amount = position.get('perp_amount_usd') or abs(remaining_pos['size'])

    result = manager.trader.close_position(
        instrument=position['instrument'],
        amount=close_amount,
        order_type='market',
    )

    if result and 'order' in result:
        logger.info(f"✅ 市價平倉已送出 [{call_inst}]")
    else:
        logger.error(f"❌ 市價平倉失敗 [{call_inst}]: {result}")

    if market_price:
        manager.update_position_fields(call_inst, close_perp_price=market_price)
        position['close_perp_price'] = market_price

    send_position_closed_notification(position, 'taker')
    manager.remove_position(call_inst)


def _emergency_market_close(manager: 'PositionManager', position: dict) -> None:
    """期權已過期，緊急市價平倉永續合約。"""
    call_inst = position.get('call_instrument', '')
    logger.warning(f"⚠️ 部位已過期 [{call_inst}]，嘗試緊急市價平倉永續...")

    # 取消殘留 Maker 單（可能 bot 重啟後狀態遺留）
    if position.get('maker_order_id'):
        manager.trader.cancel(position['maker_order_id'])

    perp_ticker     = manager.ws.get_ticker('BTC-PERPETUAL')
    emergency_price = perp_ticker.get('last_price') if perp_ticker else None

    close_amount = position.get('perp_amount_usd') or 0
    if close_amount == 0:
        actual = manager.trader.get_position(position['instrument'])
        close_amount = abs(actual.get('size', 0)) if actual else 0

    if close_amount > 0:
        result = manager.trader.close_position(
            instrument=position['instrument'],
            amount=close_amount,
            order_type='market',
        )
        if result and 'order' in result:
            logger.info(f"✅ 過期緊急平倉已送出 [{call_inst}]")
        else:
            logger.error(f"❌ 過期平倉失敗 [{call_inst}]: {result}")
    else:
        logger.info(f"ℹ️ BTC-PERPETUAL 已無倉位 [{call_inst}]，無需平倉")

    if emergency_price:
        position['close_perp_price'] = emergency_price

    send_position_closed_notification(position, 'expired')
    manager.remove_position(call_inst)
