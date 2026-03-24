# arbitrage_bot/trading_engine.py

"""
交易引擎：最終確認 + 執行套利策略。
職責：
  1. 再次驗證流動性（防止行情在掃描後變化）
  2. 以最新價格重新計算損益
  3. 呼叫 DeribitTrader 執行三腿下單
  4. 成交後更新全域狀態、部位管理器、bot_state、發送通知

Fix #1  交易互斥鎖由 scan_orchestrator 持有，進入此函式前已取得
Fix #6  funding rate 在最終確認時同步更新到 dashboard
Fix #8  下單失敗後記錄 last_failure_time，進入短暫冷卻
Fix #9  執行前確認無活躍部位（由呼叫方在鎖內雙重確認）
"""

import logging
import time
from typing import Dict

from config import Config
from deribit_api import get_funding_rate
from profit_calculator import calculate_strategy
from notifications import (
    send_trade_execution_notification,
    send_liquidity_issue_notification,
)
from bot_state import bot_state
from global_state import global_state

logger = logging.getLogger(__name__)


def perform_final_check_and_execute(
    opportunity: Dict,
    ws_client,
    trader,
    pos_manager,
) -> bool:
    """
    最終流動性確認 + 重新計算 + 執行交易。
    回傳 True 表示成交成功。
    """
    logger.info(f"⚡️ 最終確認: {opportunity['strategyName']} @ ${opportunity['strike']}")

    call_ticker  = ws_client.get_ticker(opportunity['callInstrument'])
    put_ticker   = ws_client.get_ticker(opportunity['putInstrument'])
    perp_ticker  = ws_client.get_ticker('BTC-PERPETUAL')

    if not all([call_ticker, put_ticker, perp_ticker]):
        logger.warning("最終確認失敗：無法取得最新行情")
        return False

    # ── 流動性再確認 ──────────────────────────────────────────────────────────
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

    # ── 以最新價格重算損益 ────────────────────────────────────────────────────
    price_map = {
        'A': {
            'callPrice':      call_ticker['best_bid_price'],
            'putPrice':       put_ticker['best_ask_price'],
            'perpOpenPrice':  perp_ticker['best_ask_price'],
            'perpClosePrice': perp_ticker['best_bid_price'],
        },
        'B': {
            'callPrice':      call_ticker['best_ask_price'],
            'putPrice':       put_ticker['best_bid_price'],
            'perpOpenPrice':  perp_ticker['best_bid_price'],
            'perpClosePrice': perp_ticker['best_ask_price'],
        },
    }
    updated = {**opportunity, **price_map[opportunity['strategyType']]}

    expiry_info = {
        'dateStr':   opportunity['expiryDate'],
        'fullDate':  opportunity['expiryFullDate'],
        'timestamp': opportunity['expiryTimestamp'],
    }
    funding_rate_8h = get_funding_rate(ws_client)
    bot_state.update_funding_rate(funding_rate_8h)   # Fix #6

    final = calculate_strategy(
        strategy_type   = updated['strategyType'],
        strategy_name   = updated['strategyName'],
        call_price      = updated['callPrice'],
        put_price       = updated['putPrice'],
        perp_open_price = updated['perpOpenPrice'],
        perp_close_price= updated['perpClosePrice'],
        strike          = updated['strike'],
        perpetual_price = perp_ticker['last_price'],
        funding_rate_24h= funding_rate_8h * 3,
        expiry_info     = expiry_info,
        call_instrument = updated['callInstrument'],
        put_instrument  = updated['putInstrument'],
        call_direction  = updated['callDirection'],
        put_direction   = updated['putDirection'],
        perp_direction  = updated['perpDirection'],
    )

    if final['netProfit'] < Config.MIN_NET_PROFIT_OPPORTUNITY:
        logger.warning(f"放棄：利潤已消失。最新淨利: ${final['netProfit']:.2f}")
        return False

    logger.info(f"✅ 最終確認通過！淨利: ${final['netProfit']:.2f}，準備執行")

    # ── 執行交易 ──────────────────────────────────────────────────────────────
    result = trader.execute_arbitrage_strategy(final, required_amount)
    if result and result.get('success'):
        fill_map = {o['instrument']: o.get('avg_price', 0.0) for o in result['orders']}
        final['fill_call_price'] = fill_map.get(final['callInstrument'], 0.0)
        final['fill_put_price']  = fill_map.get(final['putInstrument'],  0.0)
        final['fill_perp_price'] = fill_map.get('BTC-PERPETUAL',         0.0)
        send_trade_execution_notification(final)
        perp_amount_usd = round(required_amount * final['perpOpenPrice'] / 10) * 10
        pos_manager.add_position(
            expiry_timestamp=final['expiryTimestamp'],
            amount=required_amount,
            net_profit=final['netProfit'],
            margin=final['margin'],
            strategy_name=final.get('strategyName', ''),
            strike=final.get('strike', 0),
            call_instrument=final.get('callInstrument', ''),
            put_instrument=final.get('putInstrument', ''),
            perp_amount_usd=perp_amount_usd,
        )
        global_state.daily_trade_count += 1
        global_state.last_trade_time = time.time()
        bot_state.add_trade(final)
        return True

    # Fix #8: 記錄下單失敗時間，進入短暫冷卻
    global_state.last_failure_time = time.time()
    logger.error("❌ 交易執行失敗")
    return False
