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
import threading
import time
from typing import Dict

from config import Config
from deribit_api import get_funding_rate
from profit_calculator import calculate_strategy
from notifications import (
    _send_message,
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

    # 依實際到期剩餘小時估算資金費率，避免固定 ×3 高估/低估
    hours_to_expiry = max(1.0, (expiry_info['timestamp'] / 1000 - time.time()) / 3600)
    funding_rate_for_hold = funding_rate_8h * max(1, hours_to_expiry / 8)

    final = calculate_strategy(
        strategy_type   = updated['strategyType'],
        strategy_name   = updated['strategyName'],
        call_price      = updated['callPrice'],
        put_price       = updated['putPrice'],
        perp_open_price = updated['perpOpenPrice'],
        perp_close_price= updated['perpClosePrice'],
        strike          = updated['strike'],
        perpetual_price = perp_ticker['last_price'],
        funding_rate_24h= funding_rate_for_hold,
        expiry_info     = expiry_info,
        call_instrument = updated['callInstrument'],
        put_instrument  = updated['putInstrument'],
        call_direction  = updated['callDirection'],
        put_direction   = updated['putDirection'],
        perp_direction  = updated['perpDirection'],
        amount          = required_amount,
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
        perp_amount_usd = round(required_amount * final['perpOpenPrice'] / 10) * 10

        # ── 以實際成交價重算損益，覆蓋掃描時的估算值 ─────────────────────────
        if final['fill_call_price'] and final['fill_put_price'] and final['fill_perp_price']:
            fill_result = calculate_strategy(
                strategy_type    = final['strategyType'],
                strategy_name    = final['strategyName'],
                call_price       = final['fill_call_price'],
                put_price        = final['fill_put_price'],
                perp_open_price  = final['fill_perp_price'],
                perp_close_price = perp_ticker['last_price'],
                strike           = final['strike'],
                perpetual_price  = perp_ticker['last_price'],
                funding_rate_24h = funding_rate_for_hold,
                expiry_info      = expiry_info,
                call_instrument  = final['callInstrument'],
                put_instrument   = final['putInstrument'],
                call_direction   = final['callDirection'],
                put_direction    = final['putDirection'],
                perp_direction   = final['perpDirection'],
                amount           = required_amount,
            )
            fill_net = fill_result['netProfit']

            # ── 各腿掃描價 vs 成交價 滑點日誌 ────────────────────────────────
            scan_call = final.get('callPrice', 0.0)
            scan_put  = final.get('putPrice', 0.0)
            scan_perp = final.get('perpOpenPrice', 0.0)
            slip_call = final['fill_call_price'] - scan_call
            slip_put  = final['fill_put_price']  - scan_put
            slip_perp = final['fill_perp_price'] - scan_perp
            logger.info(
                f"📊 滑點明細 | Call: 掃描={scan_call:.4f} 成交={final['fill_call_price']:.4f} "
                f"差={slip_call:+.4f} | Put: 掃描={scan_put:.4f} 成交={final['fill_put_price']:.4f} "
                f"差={slip_put:+.4f} | Perp: 掃描=${scan_perp:.1f} 成交=${final['fill_perp_price']:.1f} "
                f"差=${slip_perp:+.1f}"
            )
            logger.info(
                f"📊 成交後實算：掃描淨利 ${final['netProfit']:.2f} → "
                f"成交後淨利 ${fill_net:.2f} (落差 ${fill_net - final['netProfit']:+.2f})"
            )

            if fill_net < 0:
                logger.warning(
                    f"⚠️  成交後淨利轉負 (${fill_net:.2f})，"
                    "成交價與掃描價存在顯著落差，建議檢查流動性。"
                )
                # 發送 Telegram 警報（非同步，不阻塞）
                alert_msg = (
                    f"⚠️ *滑點警報* ⚠️\n\n"
                    f"策略: {final['strategyName']} @ ${final['strike']}\n"
                    f"掃描淨利: ${final['netProfit']:.2f}\n"
                    f"成交淨利: ${fill_net:.2f}\n"
                    f"落差: ${fill_net - final['netProfit']:+.2f}\n\n"
                    f"Call 滑點: {slip_call:+.4f} BTC\n"
                    f"Put 滑點: {slip_put:+.4f} BTC\n"
                    f"Perp 滑點: ${slip_perp:+.1f}\n\n"
                    f"請檢查流動性與執行延遲。"
                )
                threading.Thread(target=_send_message, args=(alert_msg,), daemon=True).start()
            # 以 fill-based 數值存入部位，確保前端顯示準確
            final['grossProfit']     = fill_result['grossProfit']
            final['totalFees']       = fill_result['totalFees']
            final['fundingCost']     = fill_result['fundingCost']
            final['fundingDirection']= fill_result['fundingDirection']
            final['netProfit']       = fill_net
            final['margin']          = fill_result['margin']
            final['callFee']         = fill_result['callFee']
            final['putFee']          = fill_result['putFee']
            final['perpOpenFee']     = fill_result['perpOpenFee']
            final['perpCloseFee']    = fill_result['perpCloseFee']

        # 先登記部位（確保即使通知失敗，倉位仍被追蹤）
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
            fill_call_price=final['fill_call_price'],
            fill_put_price=final['fill_put_price'],
            fill_perp_price=final['fill_perp_price'],
            call_direction=final.get('callDirection', ''),
            put_direction=final.get('putDirection', ''),
            perp_direction=final.get('perpDirection', ''),
            gross_profit=final.get('grossProfit', 0.0),
            total_fees=final.get('totalFees', 0.0),
            funding_cost=final.get('fundingCost', 0.0),
            funding_direction=final.get('fundingDirection', ''),
            call_fee=final.get('callFee', 0.0),
            put_fee=final.get('putFee', 0.0),
            perp_open_fee=final.get('perpOpenFee', 0.0),
            perp_close_fee=final.get('perpCloseFee', 0.0),
        )
        global_state.last_trade_time = time.time()
        bot_state.add_trade(final)
        try:
            send_trade_execution_notification(final)
        except Exception as e:
            logger.error(f'❌ 成交通知發送失敗: {e}', exc_info=True)
        return True

    # Fix #8: 記錄下單失敗時間 + 類型，進入短暫冷卻
    global_state.last_failure_time = time.time()
    failure_type = (result.get('failure_type', 'exchange')
                    if isinstance(result, dict) else 'exchange')
    global_state.last_failure_type = failure_type
    logger.error(f"❌ 交易執行失敗 (type={failure_type})")
    return False
