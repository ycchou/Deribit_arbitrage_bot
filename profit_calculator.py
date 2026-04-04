# arbitrage_bot/profit_calculator.py

"""
純函數：計算套利策略的損益明細。
無副作用，不依賴外部狀態，易於單元測試。
"""

from typing import Dict

# 手續費費率常數
OPTION_TAKER_FEE_RATE = 0.0003
PERP_TAKER_FEE_RATE   = 0.0005
PERP_MAKER_FEE_RATE   = -0.0001  # 負值 = 回扣


def calculate_strategy(
    strategy_type: str, strategy_name: str,
    call_price: float, put_price: float,
    perp_open_price: float, perp_close_price: float,
    strike: float, perpetual_price: float,
    funding_rate_24h: float, expiry_info: Dict,
    call_instrument: str, put_instrument: str,
    call_direction: str, put_direction: str, perp_direction: str,
    amount: float = 1.0,
) -> Dict:
    """
    計算單一套利策略的詳細損益。
    amount 為實際交易規模（BTC），所有損益、手續費、保證金均按此規模回報。
    回傳包含 grossProfit, totalFees, fundingCost, netProfit, margin 的 dict。
    """
    contract_size = amount

    option_premium_diff = call_price - put_price
    perp_strike_diff    = (perp_open_price - strike) / perpetual_price

    if strategy_type == 'A':
        gross_profit = (option_premium_diff - perp_strike_diff) * perpetual_price * contract_size
    else:
        gross_profit = (perp_strike_diff - option_premium_diff) * perpetual_price * contract_size

    # ── 手續費計算 ──────────────────────────────────────────────────────────────
    # Deribit 期權費 = 0.03% of underlying，cap 12.5% of premium
    option_underlying  = perpetual_price * contract_size
    call_premium_usd   = call_price * perpetual_price * contract_size
    put_premium_usd    = put_price  * perpetual_price * contract_size
    call_fee = min(option_underlying * OPTION_TAKER_FEE_RATE,
                   call_premium_usd * 0.125)
    put_fee  = min(option_underlying * OPTION_TAKER_FEE_RATE,
                   put_premium_usd  * 0.125)

    perp_open_notional = perp_open_price  * contract_size
    perp_close_notional= perp_close_price * contract_size

    total_fees = (
        call_fee +
        put_fee  +
        perp_open_notional  * PERP_TAKER_FEE_RATE   +
        perp_close_notional * PERP_TAKER_FEE_RATE     # 平倉用市價單，收 Taker 費
    )

    # ── 資金費率成本 ────────────────────────────────────────────────────────────
    funding_cost_abs = perp_open_price * contract_size * abs(funding_rate_24h)

    if perp_direction == 'long':
        funding_direction = '支付' if funding_rate_24h >= 0 else '收入'
        funding_cost = funding_cost_abs if funding_rate_24h >= 0 else -funding_cost_abs
    else:
        funding_direction = '收入' if funding_rate_24h >= 0 else '支付'
        funding_cost = -funding_cost_abs if funding_rate_24h >= 0 else funding_cost_abs

    net_profit = gross_profit - total_fees - funding_cost

    # ── 保證金估算 ──────────────────────────────────────────────────────────────
    call_value    = call_price * perpetual_price * contract_size
    put_value     = put_price  * perpetual_price * contract_size
    perp_value    = perpetual_price * contract_size
    option_margin = max(call_value, put_value) * 0.15 + min(call_value, put_value)
    perp_margin   = perp_value * 0.1
    margin        = option_margin + perp_margin

    return {
        'strategyType': strategy_type,  'strategyName': strategy_name,
        'strike': strike,
        'expiryDate':      expiry_info['dateStr'],
        'expiryFullDate':  expiry_info['fullDate'],
        'expiryTimestamp': expiry_info['timestamp'],
        'callInstrument':  call_instrument,
        'putInstrument':   put_instrument,
        'callPrice':       call_price,
        'putPrice':        put_price,
        'perpOpenPrice':   perp_open_price,
        'perpClosePrice':  perp_close_price,
        'callDirection':   call_direction,
        'putDirection':    put_direction,
        'perpDirection':   perp_direction,
        'grossProfit':     gross_profit,
        'totalFees':       total_fees,
        'fundingCost':     abs(funding_cost),
        'fundingDirection':funding_direction,
        'netProfit':       net_profit,
        'margin':          margin,
        'fundingRate24h':  funding_rate_24h * 100,
    }
