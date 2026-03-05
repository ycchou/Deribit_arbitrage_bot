# arbitrage_bot/main.py

"""
程式主入口。
架構：事件驅動 — WebSocket 收到新 ticker 時立刻觸發套利掃描，
不再依賴固定時間 sleep。
"""

import time
import threading
import logging
from typing import Dict, List, Optional

from utils import setup_logging
from deribit_ws_client import DeribitWebSocket
from deribit_api import get_tomorrow_expiry, get_target_strikes, get_funding_rate
from strategy import check_arbitrage_opportunity, calculate_strategy
from notifications import send_trade_execution_notification, send_liquidity_issue_notification
from config import Config
from deribit_trader import DeribitTrader
from position_manager import PositionManager

logger = logging.getLogger(__name__)


class GlobalState:
    def __init__(self):
        self.current_instruments: set = set()
        self.last_expiry_date: Optional[str] = None
        self.last_trade_time: float = 0
        # 節流：避免同一時間連發多次掃描
        self._scan_lock = threading.Lock()
        self._last_scan_time: float = 0
        self.MIN_SCAN_INTERVAL = 0.05  # 最快 50ms 掃一次（避免 ticker flood）

    def should_scan(self) -> bool:
        """節流檢查：距上次掃描是否超過最小間隔"""
        now = time.time()
        if now - self._last_scan_time < self.MIN_SCAN_INTERVAL:
            return False
        self._last_scan_time = now
        return True


global_state = GlobalState()


def perform_final_check_and_execute(
    opportunity: Dict,
    ws_client: DeribitWebSocket,
    trader: DeribitTrader,
    pos_manager: PositionManager,
) -> bool:
    logger.info(f"⚡️ 最終確認: {opportunity['strategyName']} @ ${opportunity['strike']}")

    call_ticker = ws_client.get_ticker(opportunity['callInstrument'])
    put_ticker  = ws_client.get_ticker(opportunity['putInstrument'])
    perp_ticker = ws_client.get_ticker('BTC-PERPETUAL')

    if not all([call_ticker, put_ticker, perp_ticker]):
        logger.warning("最終確認失敗：無法取得最新行情")
        return False

    required_amount = Config.TRADE_AMOUNT_BTC
    liquidity_map = {
        'A': {
            'callAmount': call_ticker['best_bid_amount'],
            'putAmount':  put_ticker['best_ask_amount'],
            'perpAmount': perp_ticker['best_ask_amount'],
        },
        'B': {
            'callAmount': call_ticker['best_ask_amount'],
            'putAmount':  put_ticker['best_bid_amount'],
            'perpAmount': perp_ticker['best_bid_amount'],
        },
    }
    latest_liquidity = liquidity_map[opportunity['strategyType']]

    if not all(v >= required_amount for v in latest_liquidity.values()):
        opportunity.update(latest_liquidity)
        send_liquidity_issue_notification(opportunity)
        return False

    price_map = {
        'A': {
            'callPrice': call_ticker['best_bid_price'],
            'putPrice':  put_ticker['best_ask_price'],
            'perpOpenPrice':  perp_ticker['best_ask_price'],
            'perpClosePrice': perp_ticker['best_bid_price'],
        },
        'B': {
            'callPrice': call_ticker['best_ask_price'],
            'putPrice':  put_ticker['best_bid_price'],
            'perpOpenPrice':  perp_ticker['best_bid_price'],
            'perpClosePrice': perp_ticker['best_ask_price'],
        },
    }
    latest_prices = price_map[opportunity['strategyType']]
    updated = {**opportunity, **latest_prices}

    expiry_info = {
        'dateStr':   opportunity['expiryDate'],
        'fullDate':  opportunity['expiryFullDate'],
        'timestamp': opportunity['expiryTimestamp'],
    }
    funding_rate_8h = get_funding_rate(ws_client)

    final = calculate_strategy(
        strategy_type=updated['strategyType'], strategy_name=updated['strategyName'],
        call_price=updated['callPrice'], put_price=updated['putPrice'],
        perp_open_price=updated['perpOpenPrice'], perp_close_price=updated['perpClosePrice'],
        strike=updated['strike'], perpetual_price=perp_ticker['last_price'],
        funding_rate_24h=funding_rate_8h * 3, expiry_info=expiry_info,
        call_instrument=updated['callInstrument'], put_instrument=updated['putInstrument'],
        call_direction=updated['callDirection'], put_direction=updated['putDirection'],
        perp_direction=updated['perpDirection'],
    )

    if final['netProfit'] < Config.MIN_NET_PROFIT_OPPORTUNITY:
        logger.warning(f"放棄：利潤已消失。最新淨利: ${final['netProfit']:.2f}")
        return False

    logger.info(f"✅ 最終確認通過！淨利: ${final['netProfit']:.2f}，準備執行")

    result = trader.execute_arbitrage_strategy(final, required_amount)
    if result and result.get('success'):
        global_state.last_trade_time = time.time()
        send_trade_execution_notification(final)
        pos_manager.add_position(
            expiry_timestamp=final['expiryTimestamp'],
            amount=required_amount,
        )
        return True

    logger.error("❌ 交易執行失敗")
    return False


def run_scan(ws_client: DeribitWebSocket, trader: DeribitTrader,
             pos_manager: PositionManager) -> None:
    """單次掃描邏輯（從事件 callback 或兜底 loop 呼叫）"""
    try:
        # 節流
        if not global_state.should_scan():
            return

        # 冷卻期
        elapsed = time.time() - global_state.last_trade_time
        if elapsed < Config.COOLDOWN_PERIOD_SECONDS:
            remaining = (Config.COOLDOWN_PERIOD_SECONDS - elapsed) / 60
            logger.info(f"❄️ 冷卻期剩餘 {remaining:.1f} 分鐘")
            return

        perp_ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if not perp_ticker or not perp_ticker.get('last_price'):
            return

        expiry_info = get_tomorrow_expiry()
        if not expiry_info:
            return

        if expiry_info['dateStr'] != global_state.last_expiry_date:
            logger.info(f"✅ 目標到期日: {expiry_info['dateStr']} ({expiry_info['fullDate']})")
            global_state.last_expiry_date = expiry_info['dateStr']

        strikes = get_target_strikes(perp_ticker['last_price'], expiry_info['dateStr'])
        if not strikes:
            return

        # 訂閱需要的合約
        instruments_needed = []
        for strike in strikes:
            instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(strike)}-C")
            instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(strike)}-P")

        instruments_set = set(instruments_needed) | {'BTC-PERPETUAL'}
        if instruments_set != global_state.current_instruments:
            ws_client.subscribe_instruments(instruments_needed)
            global_state.current_instruments = instruments_set
            if not ws_client.wait_for_data(instruments_needed, timeout=10):
                logger.warning('⚠️ 部分數據未就緒，繼續執行')

        # 掃描套利機會
        all_opportunities: List[Dict] = []
        for strike in strikes:
            result = check_arbitrage_opportunity(strike, expiry_info, ws_client)
            if result:
                if result['strategyA'] and result['strategyA']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyA'])
                if result['strategyB'] and result['strategyB']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyB'])

        if not all_opportunities:
            logger.info(f'📊 未發現高利潤機會 (> ${Config.MIN_NET_PROFIT_OPPORTUNITY})')
            return

        best = max(all_opportunities, key=lambda x: x['netProfit'])
        logger.info(f"🏆 最佳機會: {best['strategyName']} @ ${best['strike']} 淨利=${best['netProfit']:.2f}")
        perform_final_check_and_execute(best, ws_client, trader, pos_manager)

    except Exception as e:
        logger.error(f'❌ run_scan 發生錯誤: {e}', exc_info=True)


if __name__ == '__main__':
    setup_logging()
    logger.info('🤖 Deribit 套利機器人啟動')

    ws_client = DeribitWebSocket()
    ws_client.start()

    logger.info('⏳ 等待 WebSocket 連接與認證...')
    if not ws_client.wait_for_connection(timeout=15):
        logger.error('❌ WebSocket 連接超時，程式退出')
        ws_client.stop()
        exit(1)
    logger.info('✅ WebSocket 已就緒')

    # DeribitTrader 現在只需 ws_client（不再需要 REST credentials）
    trader = DeribitTrader(ws_client)
    pos_manager = PositionManager(trader, ws_client)
    pos_manager.start()

    # 訂閱 BTC-PERPETUAL
    ws_client.subscribe_instruments(['BTC-PERPETUAL'])
    if not ws_client.wait_for_data(['BTC-PERPETUAL'], timeout=10):
        logger.error('❌ 無法取得 BTC-PERPETUAL 初始數據，程式退出')
        ws_client.stop()
        pos_manager.stop()
        exit(1)
    logger.info('✅ BTC-PERPETUAL 數據已就緒\n')

    # ── 事件驅動：每次收到 BTC-PERPETUAL ticker 就觸發掃描 ──────────────────
    def on_ticker(instrument: str) -> None:
        if instrument == 'BTC-PERPETUAL':
            run_scan(ws_client, trader, pos_manager)

    ws_client.set_on_ticker_update(on_ticker)
    logger.info('🎯 事件驅動模式已啟動，等待市場數據...')

    # ── 兜底 loop：確保即使 ticker 長時間不更新也會定期掃描 ──────────────────
    try:
        iteration = 0
        while True:
            time.sleep(Config.SCAN_INTERVAL_SECONDS)
            iteration += 1
            if ws_client.is_connected:
                logger.debug(f'🔄 兜底掃描 #{iteration}')
                run_scan(ws_client, trader, pos_manager)
            else:
                logger.warning('⚠️ WebSocket 未連接，跳過兜底掃描')
    except KeyboardInterrupt:
        logger.info('\n👋 正在停止...')
    finally:
        ws_client.stop()
        pos_manager.stop()
        logger.info('✅ 程式已安全停止')
