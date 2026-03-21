# arbitrage_bot/strategy.py

"""
機會偵測：掃描指定履約價的套利機會。
計算邏輯已移至 profit_calculator.py（純函數）。

向後相容：其他模組 import calculate_strategy 仍可正常使用。
"""

import logging
from typing import Dict, Optional, TYPE_CHECKING

from config import Config
from deribit_api import get_funding_rate
from profit_calculator import calculate_strategy  # 核心計算移至獨立模組

if TYPE_CHECKING:
    from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)

# 重新匯出，讓現有 import 不需修改
__all__ = ['calculate_strategy', 'check_arbitrage_opportunity']


def check_arbitrage_opportunity(
    strike: float,
    expiry_info: Dict,
    ws_client: 'DeribitWebSocket',
) -> Optional[Dict]:
    """
    以 WebSocket 即時數據掃描指定履約價的套利機會。
    回傳 {'strike', 'strategyA', 'strategyB'} 或 None。
    """
    try:
        call_instrument = f"BTC-{expiry_info['dateStr']}-{int(strike)}-C"
        put_instrument  = f"BTC-{expiry_info['dateStr']}-{int(strike)}-P"

        call_ticker      = ws_client.get_ticker(call_instrument)
        put_ticker       = ws_client.get_ticker(put_instrument)
        perpetual_ticker = ws_client.get_ticker('BTC-PERPETUAL')

        if not all([call_ticker, put_ticker, perpetual_ticker]):
            return None

        required_fields = [
            'best_bid_price', 'best_ask_price', 'last_price',
            'best_bid_amount', 'best_ask_amount',
        ]
        if not all(
            f in ticker and ticker[f] is not None and ticker[f] > 0
            for ticker in [call_ticker, put_ticker, perpetual_ticker]
            for f in required_fields
        ):
            return None

        funding_rate_8h  = get_funding_rate(ws_client)
        funding_rate_24h = funding_rate_8h * 3
        perpetual_price  = perpetual_ticker['last_price']

        # ── 策略 A：賣 Call + 買 Put + 買 Perp ───────────────────────────────
        strategy_a = calculate_strategy(
            'A', '策略A (賣Call+買Put+買Perp)',
            call_ticker['best_bid_price'], put_ticker['best_ask_price'],
            perpetual_ticker['best_ask_price'], perpetual_ticker['best_bid_price'],
            strike, perpetual_price, funding_rate_24h, expiry_info,
            call_instrument, put_instrument, 'sell', 'buy', 'long',
        )
        liquidity_a_ok = all([
            call_ticker['best_bid_amount'] >= Config.TRADE_AMOUNT_BTC,
            put_ticker['best_ask_amount']  >= Config.TRADE_AMOUNT_BTC,
            perpetual_ticker['best_ask_amount'] >= Config.TRADE_AMOUNT_BTC,
        ])

        # ── 策略 B：買 Call + 賣 Put + 賣 Perp ───────────────────────────────
        strategy_b = calculate_strategy(
            'B', '策略B (買Call+賣Put+賣Perp)',
            call_ticker['best_ask_price'], put_ticker['best_bid_price'],
            perpetual_ticker['best_bid_price'], perpetual_ticker['best_ask_price'],
            strike, perpetual_price, funding_rate_24h, expiry_info,
            call_instrument, put_instrument, 'buy', 'sell', 'short',
        )
        liquidity_b_ok = all([
            call_ticker['best_ask_amount'] >= Config.TRADE_AMOUNT_BTC,
            put_ticker['best_bid_amount']  >= Config.TRADE_AMOUNT_BTC,
            perpetual_ticker['best_bid_amount'] >= Config.TRADE_AMOUNT_BTC,
        ])

        # ── 日誌 ──────────────────────────────────────────────────────────────
        logger.debug(
            f"掃描 @ ${strike:<6} | "
            f"策略A 淨利: ${strategy_a['netProfit']:<8.2f} (流動性: {'OK' if liquidity_a_ok else '不足'}) | "
            f"策略B 淨利: ${strategy_b['netProfit']:<8.2f} (流動性: {'OK' if liquidity_b_ok else '不足'})"
        )

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

        return {
            'strike':    strike,
            'strategyA': strategy_a if liquidity_a_ok else None,
            'strategyB': strategy_b if liquidity_b_ok else None,
        }

    except Exception as e:
        logger.error(f'❌ 檢查履約價 {strike} 時發生錯誤: {e}', exc_info=True)
        return None
