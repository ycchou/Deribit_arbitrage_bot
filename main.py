# arbitrage_bot/main.py

"""
程式主入口。
負責初始化、協調各模組，並控制主執行循環。
"""
import time
import logging
from typing import Dict, List, Optional

from utils import setup_logging
from deribit_ws_client import DeribitWebSocket
from deribit_api import get_tomorrow_expiry, get_target_strikes
from strategy import check_arbitrage_opportunity, calculate_strategy
from notifications import send_trade_execution_notification, send_liquidity_issue_notification
from config import Config
from deribit_trader import DeribitTrader
from position_manager import PositionManager

logger = logging.getLogger(__name__)

class GlobalState:
    """管理主循環的狀態，避免重複日誌或訂閱"""
    def __init__(self):
        self.current_instruments = set()
        self.last_expiry_date = None
        self.last_trade_time = 0  # 上次成功交易的時間戳

global_state = GlobalState()

def perform_final_check_and_execute(
    opportunity: Dict, 
    ws_client: DeribitWebSocket, 
    trader: DeribitTrader, 
    pos_manager: PositionManager
) -> bool:
    """
    在執行交易前的最後一刻，重新獲取市場數據進行最終確認。
    如果確認通過，則立即執行交易。
    """
    logger.info(f"⚡️ 執行最終確認: {opportunity['strategyName']} @ ${opportunity['strike']}")
    
    # 1. 立即重新獲取最新的市場數據
    call_ticker = ws_client.get_ticker(opportunity['callInstrument'])
    put_ticker = ws_client.get_ticker(opportunity['putInstrument'])
    perp_ticker = ws_client.get_ticker('BTC-PERPETUAL')

    if not all([call_ticker, put_ticker, perp_ticker]):
        logger.warning("最終確認失敗：無法獲取最新市場行情。")
        return False
        
    # 2. 根據策略方向，確定最新的掛單簿深度
    required_amount = Config.TRADE_AMOUNT_BTC
    liquidity_map = {
        'A': { # 賣Call, 買Put, 買Perp
            'callAmount': call_ticker['best_bid_amount'],
            'putAmount': put_ticker['best_ask_amount'],
            'perpAmount': perp_ticker['best_ask_amount']
        },
        'B': { # 買Call, 賣Put, 賣Perp
            'callAmount': call_ticker['best_ask_amount'],
            'putAmount': put_ticker['best_bid_amount'],
            'perpAmount': perp_ticker['best_bid_amount']
        }
    }
    latest_liquidity = liquidity_map[opportunity['strategyType']]

    # 3. 進行最終的流動性檢查
    if not all(amount >= required_amount for amount in latest_liquidity.values()):
        opportunity.update(latest_liquidity) # 更新流動性數據以便發送通知
        send_liquidity_issue_notification(opportunity)
        return False

    # 4. 使用最新的價格重新計算淨利潤，確保機會依然存在
    # 根據策略方向，確定最新的交易價格
    price_map = {
        'A': {
            'callPrice': call_ticker['best_bid_price'],
            'putPrice': put_ticker['best_ask_price'],
            'perpOpenPrice': perp_ticker['best_ask_price'],
            'perpClosePrice': perp_ticker['best_bid_price'],
        },
        'B': {
            'callPrice': call_ticker['best_ask_price'],
            'putPrice': put_ticker['best_bid_price'],
            'perpOpenPrice': perp_ticker['best_bid_price'],
            'perpClosePrice': perp_ticker['best_ask_price'],
        }
    }
    latest_prices = price_map[opportunity['strategyType']]
    
    # 更新 opportunity 物件，使用最新的價格來重新計算
    updated_opportunity = opportunity.copy()
    updated_opportunity.update(latest_prices)

    # 重新計算利潤 (這裡可以簡化，或調用一個輕量級的重算函式)
    # 為求精確，我們直接複用 calculate_strategy 的邏輯
    # 注意：這裡的 expiry_info 和 funding_rate 可能有微小延遲，但在秒級決策中可接受
    from deribit_api import get_funding_rate # 臨時導入
    expiry_info = {'dateStr': opportunity['expiryDate'], 'fullDate': opportunity['expiryFullDate'], 'timestamp': opportunity['expiryTimestamp']}
    funding_rate_8h = get_funding_rate(ws_client)
    
    final_check_opportunity = calculate_strategy(
        strategy_type=updated_opportunity['strategyType'], strategy_name=updated_opportunity['strategyName'],
        call_price=updated_opportunity['callPrice'], put_price=updated_opportunity['putPrice'],
        perp_open_price=updated_opportunity['perpOpenPrice'], perp_close_price=updated_opportunity['perpClosePrice'],
        strike=updated_opportunity['strike'], perpetual_price=perp_ticker['last_price'],
        funding_rate_24h=funding_rate_8h * 3, expiry_info=expiry_info,
        call_instrument=updated_opportunity['callInstrument'], put_instrument=updated_opportunity['putInstrument'],
        call_direction=updated_opportunity['callDirection'], put_direction=updated_opportunity['putDirection'],
        perp_direction=updated_opportunity['perpDirection']
    )

    # --- 修改點：使用 Config 中的淨利潤門檻 ---
    if final_check_opportunity['netProfit'] < Config.MIN_NET_PROFIT_OPPORTUNITY:
        logger.warning(f"放棄交易：利潤已消失。最新預估淨利: ${final_check_opportunity['netProfit']:.2f}")
        return False
        
    logger.info(f"✅ 最終確認通過！最新預估淨利: ${final_check_opportunity['netProfit']:.2f}。準備執行！")
    
    # 5. 所有檢查通過，立即執行交易
    execution_result = trader.execute_arbitrage_strategy(final_check_opportunity, required_amount)

    if execution_result and execution_result.get('success'):
        global_state.last_trade_time = time.time()
        send_trade_execution_notification(final_check_opportunity)
        pos_manager.add_position(
            expiry_timestamp=final_check_opportunity['expiryTimestamp'],
            amount=required_amount
        )
        return True
    else:
        logger.error("❌ 交易執行失敗，請檢查日誌獲取詳細資訊。")
        return False


def main_loop(ws_client: DeribitWebSocket, trader: DeribitTrader, pos_manager: PositionManager):
    """主要執行循環"""
    try:
        # 1. 檢查是否在冷卻期
        time_since_last_trade = time.time() - global_state.last_trade_time
        if time_since_last_trade < Config.COOLDOWN_PERIOD_SECONDS:
            remaining_cooldown = Config.COOLDOWN_PERIOD_SECONDS - time_since_last_trade
            logger.info(f"❄️ 處於交易冷卻期，剩餘 {remaining_cooldown/60:.1f} 分鐘")
            return

        # 2. 獲取市場基礎數據
        perpetual_ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if not perpetual_ticker or not perpetual_ticker.get('last_price'):
            logger.warning('⚠️ WebSocket 永續數據暫時不可用，跳過本次掃描')
            return
        
        expiry_info = get_tomorrow_expiry()
        if not expiry_info:
            logger.info('❌ 未找到明天到期的合約')
            return
        
        if expiry_info['dateStr'] != global_state.last_expiry_date:
            logger.info(f"✅ 目標到期日: {expiry_info['dateStr']} ({expiry_info['fullDate']})")
            global_state.last_expiry_date = expiry_info['dateStr']
        
        strikes = get_target_strikes(perpetual_ticker['last_price'], expiry_info['dateStr'])
        if not strikes:
            logger.info('❌ 沒有符合條件的履約價')
            return
        
        # 3. 更新 WebSocket 訂閱
        instruments_needed = []
        for strike in strikes:
            instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(strike)}-C")
            instruments_needed.append(f"BTC-{expiry_info['dateStr']}-{int(strike)}-P")
        
        instruments_set = set(instruments_needed)
        instruments_set.add('BTC-PERPETUAL')

        if instruments_set != global_state.current_instruments:
            ws_client.subscribe_instruments(instruments_needed) 
            global_state.current_instruments = instruments_set
            if not ws_client.wait_for_data(instruments_needed, timeout=10):
                logger.warning('⚠️ 部分數據未就緒，但將繼續執行')
        
        # 4. (掃描階段) 找出所有潛在機會
        all_opportunities: List[Dict] = []
        for strike in strikes:
            result = check_arbitrage_opportunity(strike, expiry_info, ws_client)
            if result:
                # --- 修改點：使用 Config 中的淨利潤門檻 ---
                if result['strategyA'] and result['strategyA']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyA'])
                # --- 修改點：使用 Config 中的淨利潤門檻 ---
                if result['strategyB'] and result['strategyB']['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
                    all_opportunities.append(result['strategyB'])

        if not all_opportunities:
            # --- 修改點：使用 Config 中的淨利潤門檻，動態更新日誌訊息 ---
            logger.info(f'📊 本輪掃描未發現高利潤機會 (> ${Config.MIN_NET_PROFIT_OPPORTUNITY})。')
            return

        # 5. (選取階段) 從所有機會中，選出理論利潤最高的一個
        best_opportunity = max(all_opportunities, key=lambda x: x['netProfit'])
        logger.info(f"🏆 最高理論利潤機會: {best_opportunity['strategyName']} @ ${best_opportunity['strike']} (理論淨利: ${best_opportunity['netProfit']:.2f})")

        # 6. (確認與執行階段) 進行最終確認並嘗試執行
        perform_final_check_and_execute(best_opportunity, ws_client, trader, pos_manager)

    except Exception as e:
        logger.error(f'❌ main_loop 發生錯誤: {e}', exc_info=True)


if __name__ == '__main__':
    setup_logging()
    logger.info('🤖 Deribit 套利交易機器人啟動')

    ws_client = DeribitWebSocket()
    ws_client.start()
    
    logger.info('⏳ 等待 WebSocket 連接...')
    if not ws_client.wait_for_connection(timeout=15):
        logger.error('❌ WebSocket 連接超時，程式退出。')
        ws_client.stop()
        exit()
    logger.info('✅ WebSocket 已就緒')

    trader = DeribitTrader(Config.DERIBIT_CLIENT_ID, Config.DERIBIT_CLIENT_SECRET)
    pos_manager = PositionManager(trader, ws_client)
    pos_manager.start()

    logger.info('📡 訂閱 BTC-PERPETUAL 數據...')
    ws_client.subscribe_instruments(['BTC-PERPETUAL'])
    global_state.current_instruments.add('BTC-PERPETUAL')
    if not ws_client.wait_for_data(['BTC-PERPETUAL'], timeout=10):
        logger.error('❌ 無法獲取 BTC-PERPETUAL 初始數據，程式退出。')
        ws_client.stop()
        pos_manager.stop()
        exit()
    logger.info('✅ BTC-PERPETUAL 數據已就緒\n')

    try:
        iteration = 0
        while True:
            loop_start_time = time.time()
            iteration += 1
            logger.info(f'🔄 第 {iteration} 次掃描...')
            
            if ws_client.is_connected:
                main_loop(ws_client, trader, pos_manager)
            else:
                logger.warning('⚠️ WebSocket 未連接，跳過本次掃描')

            loop_execution_time_ms = (time.time() - loop_start_time) * 1000
            logger.info(f'⏱️ 本輪總執行時間: {loop_execution_time_ms:.0f}ms')
            # --- 修改點 START：使用 Config 中的掃描間隔 ---
            logger.info(f'⏳ 等待 {Config.SCAN_INTERVAL_SECONDS} 秒...\n' + '='*60)
            time.sleep(Config.SCAN_INTERVAL_SECONDS)
            # --- 修改點 END ---
            
    except KeyboardInterrupt:
        logger.info('\n👋 收到關閉信號，正在優雅地停止程式...')
    finally:
        ws_client.stop()
        pos_manager.stop()
        logger.info('✅ 程式已安全停止')