# arbitrage_bot/strategy.py

"""
包含套利策略的核心計算邏輯。
這些函式負責根據市場數據計算潛在的利潤和風險。
"""
import logging
from typing import Dict, Optional, TYPE_CHECKING

# 新增：導入 Config 以獲取交易單位設定
from config import Config
from deribit_api import get_funding_rate

if TYPE_CHECKING:
    from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)

# calculate_strategy 函數保持不變
def calculate_strategy(
    strategy_type: str, strategy_name: str,
    call_price: float, put_price: float,
    perp_open_price: float, perp_close_price: float,
    strike: float, perpetual_price: float,
    funding_rate_24h: float, expiry_info: Dict,
    call_instrument: str, put_instrument: str,
    call_direction: str, put_direction: str, perp_direction: str
) -> Dict:
    """計算單一策略的詳細損益"""
    contract_size = 1
    
    option_premium_diff = call_price - put_price
    perp_strike_diff = (perp_open_price - strike) / perpetual_price
    
    if strategy_type == 'A':
        gross_profit = (option_premium_diff - perp_strike_diff) * perpetual_price
    else: # Strategy 'B'
        gross_profit = (perp_strike_diff - option_premium_diff) * perpetual_price
    
    option_taker_fee_rate = 0.0003
    perp_taker_fee_rate = 0.0005
    
    call_notional = call_price * perpetual_price * contract_size
    put_notional = put_price * perpetual_price * contract_size
    perp_open_notional = perp_open_price * contract_size
    perp_close_notional = perp_close_price * contract_size
    
    total_fees = (
        call_notional * option_taker_fee_rate +
        put_notional * option_taker_fee_rate +
        perp_open_notional * perp_taker_fee_rate +
        perp_close_notional * perp_taker_fee_rate
    )
    
    funding_cost_abs = perp_open_price * contract_size * abs(funding_rate_24h)
    
    if perp_direction == 'long':
        funding_direction = '支付' if funding_rate_24h >= 0 else '收入'
        funding_cost = funding_cost_abs if funding_rate_24h >= 0 else -funding_cost_abs
    else: # short
        funding_direction = '收入' if funding_rate_24h >= 0 else '支付'
        funding_cost = -funding_cost_abs if funding_rate_24h >= 0 else funding_cost_abs
    
    net_profit = gross_profit - total_fees - funding_cost
    
    # 估算保證金 (簡易估算)
    call_value = call_price * perpetual_price * contract_size
    put_value = put_price * perpetual_price * contract_size
    perp_value = perpetual_price * contract_size
    option_margin = max(call_value, put_value) * 0.15 + min(call_value, put_value)
    perp_margin = perp_value * 0.1
    margin = option_margin + perp_margin
    
    strategy_data = {
        'strategyType': strategy_type, 'strategyName': strategy_name, 'strike': strike,
        'expiryDate': expiry_info['dateStr'], 'expiryFullDate': expiry_info['fullDate'],
        'expiryTimestamp': expiry_info['timestamp'],
        'callInstrument': call_instrument, 'putInstrument': put_instrument,
        'callPrice': call_price, 'putPrice': put_price,
        'perpOpenPrice': perp_open_price, 'perpClosePrice': perp_close_price,
        'callDirection': call_direction, 'putDirection': put_direction, 'perpDirection': perp_direction,
        'grossProfit': gross_profit, 'totalFees': total_fees,
        'fundingCost': abs(funding_cost), 'fundingDirection': funding_direction,
        'netProfit': net_profit, 'margin': margin,
        'fundingRate24h': funding_rate_24h * 100
    }
    return strategy_data

# --- 主要修改點在此函數 ---
def check_arbitrage_opportunity(strike: float, expiry_info: Dict, ws_client: 'DeribitWebSocket') -> Optional[Dict]:
    """
    使用 WebSocket 的即時數據，掃描並計算指定履約價的套利機會利潤。
    (修改版：已包含初步流動性檢查，並會產生特殊格式的機會日誌)
    """
    try:
        call_instrument = f"BTC-{expiry_info['dateStr']}-{int(strike)}-C"
        put_instrument = f"BTC-{expiry_info['dateStr']}-{int(strike)}-P"
        perp_instrument = 'BTC-PERPETUAL'
        
        call_ticker = ws_client.get_ticker(call_instrument)
        put_ticker = ws_client.get_ticker(put_instrument)
        perpetual_ticker = ws_client.get_ticker(perp_instrument)
        
        if not all([call_ticker, put_ticker, perpetual_ticker]):
            return None
        
        required_fields = ['best_bid_price', 'best_ask_price', 'last_price', 'best_bid_amount', 'best_ask_amount']
        if not all(f in ticker and ticker[f] is not None and ticker[f] > 0 for ticker in [call_ticker, put_ticker, perpetual_ticker] for f in required_fields):
             return None

        funding_rate_8h = get_funding_rate(ws_client)
        funding_rate_24h = funding_rate_8h * 3
        perpetual_price = perpetual_ticker['last_price']
        
        # --- 策略A ---
        strategy_a = calculate_strategy(
            'A', '策略A (賣Call+買Put+買Perp)',
            call_ticker['best_bid_price'], put_ticker['best_ask_price'],
            perpetual_ticker['best_ask_price'], perpetual_ticker['best_bid_price'],
            strike, perpetual_price, funding_rate_24h, expiry_info,
            call_instrument, put_instrument, 'sell', 'buy', 'long'
        )
        liquidity_a_ok = all([
            call_ticker['best_bid_amount'] >= Config.TRADE_AMOUNT_BTC,
            put_ticker['best_ask_amount'] >= Config.TRADE_AMOUNT_BTC,
            perpetual_ticker['best_ask_amount'] >= Config.TRADE_AMOUNT_BTC
        ])
        
        # --- 策略B ---
        strategy_b = calculate_strategy(
            'B', '策略B (買Call+賣Put+賣Perp)',
            call_ticker['best_ask_price'], put_ticker['best_bid_price'],
            perpetual_ticker['best_bid_price'], perpetual_ticker['best_ask_price'],
            strike, perpetual_price, funding_rate_24h, expiry_info,
            call_instrument, put_instrument, 'buy', 'sell', 'short'
        )
        liquidity_b_ok = all([
            call_ticker['best_ask_amount'] >= Config.TRADE_AMOUNT_BTC,
            put_ticker['best_bid_amount'] >= Config.TRADE_AMOUNT_BTC,
            perpetual_ticker['best_bid_amount'] >= Config.TRADE_AMOUNT_BTC
        ])

        # --- 修改點 START：日誌記錄邏輯 ---
        
        # 1. 將常規掃描日誌降級為 DEBUG，預設不會顯示，讓主日誌更乾淨
        logger.debug(
            f"掃描 @ ${strike:<6} | "
            f"策略A 淨利: ${strategy_a['netProfit']:<8.2f} (流動性: {'OK' if liquidity_a_ok else '不足'}) | "
            f"策略B 淨利: ${strategy_b['netProfit']:<8.2f} (流動性: {'OK' if liquidity_b_ok else '不足'})"
        )
        
        # 2. 檢查是否有高利潤機會，若有，則產生帶有特殊標籤的 INFO 日誌
        if liquidity_a_ok and strategy_a['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
            logger.info(
                f"[OPPORTUNITY] 🏆 {strategy_a['strategyName']} @ ${strike} | "
                f"淨利: ${strategy_a['netProfit']:.2f}"
            )

        if liquidity_b_ok and strategy_b['netProfit'] > Config.MIN_NET_PROFIT_OPPORTUNITY:
            logger.info(
                f"[OPPORTUNITY] 🏆 {strategy_b['strategyName']} @ ${strike} | "
                f"淨利: ${strategy_b['netProfit']:.2f}"
            )
            
        # --- 修改點 END ---

        return {
            'strike': strike, 
            'strategyA': strategy_a if liquidity_a_ok else None, 
            'strategyB': strategy_b if liquidity_b_ok else None
        }
    except Exception as e:
        logger.error(f'❌ 檢查履約價 {strike} 時發生錯誤: {e}', exc_info=True)
        return None