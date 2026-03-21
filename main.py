# arbitrage_bot/main.py

"""
程式主入口：初始化所有元件並啟動事件驅動掃描循環。
業務邏輯已分散至各專責模組：
  global_state.py      — 執行期共用狀態
  scan_orchestrator.py — 掃描協調邏輯
  trading_engine.py    — 最終確認與執行
"""

import logging
import time

from utils import setup_logging
from deribit_ws_client import DeribitWebSocket
from config import Config
from deribit_trader import DeribitTrader
from position_manager import PositionManager
from bot_state import bot_state, BotStateLogHandler
from live_server import start_live_server
from notifications import send_startup_notification
from global_state import global_state
from scan_orchestrator import run_scan

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging()

    # 讓所有 log 同時推送到 live dashboard
    log_handler = BotStateLogHandler(bot_state)
    log_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(log_handler)

    logger.info('🤖 Deribit 套利機器人啟動')

    # 啟動 live dashboard
    start_live_server(bot_state)

    # 建立 WebSocket 連線
    ws_client = DeribitWebSocket()
    ws_client.start()
    logger.info('⏳ 等待 WebSocket 連接與認證...')
    if not ws_client.wait_for_connection(timeout=15):
        logger.error('❌ WebSocket 連接超時，程式退出')
        ws_client.stop()
        raise SystemExit(1)
    logger.info('✅ WebSocket 已就緒')

    # 建立交易與部位管理元件
    trader      = DeribitTrader(ws_client)
    pos_manager = PositionManager(trader, ws_client)
    pos_manager.start()

    # 訂閱 BTC-PERPETUAL 並等待初始數據
    ws_client.subscribe_instruments(['BTC-PERPETUAL'])
    if not ws_client.wait_for_data(['BTC-PERPETUAL'], timeout=10):
        logger.error('❌ 無法取得 BTC-PERPETUAL 初始數據，程式退出')
        ws_client.stop()
        pos_manager.stop()
        raise SystemExit(1)
    logger.info('✅ BTC-PERPETUAL 數據已就緒\n')

    send_startup_notification()

    # ── 事件驅動：每次收到 BTC-PERPETUAL ticker 就觸發掃描 ─────────────────────
    def on_ticker(instrument: str) -> None:
        if instrument == 'BTC-PERPETUAL':
            run_scan(ws_client, trader, pos_manager)

    ws_client.set_on_ticker_update(on_ticker)
    logger.info('🎯 事件驅動模式已啟動，等待市場數據...')

    # ── 兜底 loop：確保即使 ticker 長時間不更新也會定期掃描 ─────────────────────
    try:
        iteration = 0
        while True:
            time.sleep(Config.SCAN_INTERVAL_SECONDS)
            iteration += 1
            bot_state.update_ws_status(ws_client.is_connected, ws_client.is_authenticated)
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


if __name__ == '__main__':
    main()
