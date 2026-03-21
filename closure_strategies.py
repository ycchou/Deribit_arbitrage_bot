# arbitrage_bot/closure_strategies.py

"""
三階段平倉策略：
  階段一 → _try_close_maker   : 在平倉窗口掛 Maker（post-only）單
  階段二 → （由 WebSocket event 觸發，PositionManager 監聽）
  階段三 → _force_close_taker : 到期前強制 Taker 平倉

所有函式接收 PositionManager 實例，透過其屬性存取共用狀態，
避免傳遞大量參數。
"""

import time
import logging
from typing import TYPE_CHECKING

from config import Config
from bot_state import bot_state
from state_store import save as save_state

if TYPE_CHECKING:
    from position_manager import PositionManager

logger = logging.getLogger(__name__)


def manage_closure(manager: 'PositionManager', position: dict) -> None:
    """
    根據剩餘到期時間決定執行哪個平倉階段。
    由 PositionManager._run() 每秒呼叫一次。
    """
    now            = int(time.time())
    expiry_sec     = position['expiry_timestamp'] / 1000
    time_to_expiry = expiry_sec - now

    # 已過期未平倉
    if time_to_expiry <= 0 and position['status'] != 'closed':
        logger.warning(f"⚠️ 部位已過期但未完成平倉，狀態: {position['status']}")
        with manager.lock:
            manager.active_position = None
        bot_state.update_active_position(None)
        save_state('active_position', None)
        return

    # 階段一：進入平倉窗口，掛 Maker 單
    if (Config.POSITION_CLOSE_TRIGGER_SECONDS >= time_to_expiry > Config.TAKER_FORCE_CLOSE_SECONDS
            and position['status'] == 'monitoring'):
        logger.info(f"⏳ 進入平倉窗口（剩餘 {time_to_expiry:.0f}s），掛 Maker 平倉單...")
        _try_close_maker(manager, position)

    # 階段二：等待 Maker 成交（非阻塞查詢）
    elif position['status'] == 'closing_maker':
        if manager._maker_order_filled.wait(timeout=0):
            logger.info("✅ Maker 平倉已完成")
            with manager.lock:
                manager.active_position = None
            manager._maker_order_filled.clear()
            bot_state.update_active_position(None)
            save_state('active_position', None)

    # 階段三：強制 Taker 平倉
    elif (0 < time_to_expiry <= Config.TAKER_FORCE_CLOSE_SECONDS
          and position['status'] in ('monitoring', 'closing_maker')):
        logger.warning(f"🚨 強制 Taker 平倉（剩餘 {time_to_expiry:.0f}s）")
        _force_close_taker(manager, position)


def _try_close_maker(manager: 'PositionManager', position: dict) -> None:
    """掛出 post-only Maker 平倉單。"""
    perp_ticker = manager.ws.get_ticker('BTC-PERPETUAL')
    if not perp_ticker:
        logger.warning("無法取得 BTC-PERPETUAL ticker，跳過本次 Maker 平倉嘗試")
        return

    current_pos = manager.trader.get_position(position['instrument'])
    if not current_pos or current_pos.get('size', 0) == 0:
        logger.info("ℹ️ 部位已不存在，標記已關閉")
        with manager.lock:
            manager.active_position = None
        bot_state.update_active_position(None)
        save_state('active_position', None)
        return

    price = (perp_ticker['best_ask_price'] if current_pos['size'] < 0
             else perp_ticker['best_bid_price'])

    result = manager.trader.close_position(
        instrument=position['instrument'],
        amount=abs(current_pos['size']),
        price=price,
        post_only=True,
    )

    if result and 'order' in result:
        order_id    = result['order']['order_id']
        updated_pos = {**position, 'status': 'closing_maker', 'maker_order_id': order_id}
        logger.info(f"✅ Maker 平倉單已掛出 order_id={order_id}")
        manager._maker_order_filled.clear()
        with manager.lock:
            manager.active_position['status']         = 'closing_maker'
            manager.active_position['maker_order_id'] = order_id
        bot_state.update_active_position(updated_pos)
        save_state('active_position', updated_pos)
    else:
        logger.error(f"❌ Maker 平倉單失敗: {result}")


def _force_close_taker(manager: 'PositionManager', position: dict) -> None:
    """
    強制 Taker 平倉。
    Fix #3：先確認 Maker 單已取消，再送 Taker，避免雙倉。
    """
    if position['status'] == 'closing_maker' and position.get('maker_order_id'):
        cancel_result = manager.trader.cancel(position['maker_order_id'])
        if not cancel_result:
            logger.warning("⚠️ Maker 單取消指令無回應，查詢確認...")
        time.sleep(0.5)  # 讓取消生效

        open_orders = manager.trader.get_open_orders_by_instrument(position['instrument'])
        if any(o.get('order_id') == position['maker_order_id'] for o in open_orders):
            logger.error(
                f"❌ Maker 單 {position['maker_order_id']} 仍在掛單中，"
                "中止 Taker 平倉以避免重複建倉"
            )
            return

    remaining_pos = manager.trader.get_position(position['instrument'])
    if not remaining_pos or remaining_pos.get('size', 0) == 0:
        logger.info("ℹ️ Taker 平倉前部位已不存在")
        with manager.lock:
            manager.active_position = None
        bot_state.update_active_position(None)
        save_state('active_position', None)
        return

    perp_ticker     = manager.ws.get_ticker('BTC-PERPETUAL')
    multiplier      = 0.995 if remaining_pos['size'] < 0 else 1.005
    aggressive_price= perp_ticker['last_price'] * multiplier

    result = manager.trader.close_position(
        instrument=position['instrument'],
        amount=abs(remaining_pos['size']),
        price=aggressive_price,
    )

    if result and 'order' in result:
        logger.info("✅✅ Taker 強制平倉單已送出")
    else:
        logger.error(f"❌❌ Taker 強制平倉失敗: {result}")

    with manager.lock:
        manager.active_position = None
    bot_state.update_active_position(None)
    save_state('active_position', None)
