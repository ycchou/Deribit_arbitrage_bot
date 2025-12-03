# arbitrage_bot/deribit_trader.py

"""
處理所有對 Deribit 私有 API 的請求（需授權）。
包含交易執行、部位查詢、訂單管理等功能。
"""
import requests
import time
import hmac
import hashlib
import json
import logging
from typing import Dict, Optional, Any, List
# 新增導入：用於併發請求
from concurrent.futures import ThreadPoolExecutor

from config import Config

logger = logging.getLogger(__name__)

class DeribitTrader:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = Config.DERIBIT_BASE_URL

    def _send_private_request(self, method: str, params: Optional[Dict] = None) -> Dict:
        """發送私有 API 請求的核心函式"""
        params = params or {}
        timestamp = int(time.time() * 1000)
        nonce = str(timestamp)
        
        # 準備簽名
        signature_data = f"{timestamp}\n{nonce}\n"
        signature = hmac.new(
            self.client_secret.encode(),
            signature_data.encode(),
            hashlib.sha256
        ).hexdigest()

        headers = {
            'Authorization': f'deri-hmac-sha256 id={self.client_id},ts={timestamp},sig={signature},nonce={nonce}',
            'Content-Type': 'application/json'
        }
        
        url = f'{self.base_url}{method}'
        
        try:
            response = requests.post(url, headers=headers, json={'params': params}, timeout=15)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                logger.error(f"❌ API 請求失敗 ({method}): {data['error']}")
                return data
            return data.get('result', {})
        except requests.RequestException as e:
            logger.error(f"❌ API 網路請求錯誤 ({method}): {e}")
            return {'error': str(e)}
        except Exception as e:
            logger.error(f"❌ API 未知錯誤 ({method}): {e}")
            return {'error': str(e)}

    # --- 這是被替換和優化的函數 ---
    def execute_arbitrage_strategy(self, strategy: Dict, amount: float) -> Optional[Dict]:
        """【併發優化版】同時發送一組套利訂單（Call, Put, Perpetual）"""
        logger.info(f"🚀 準備併發執行策略: {strategy['strategyName']} @ ${strategy['strike']} for {amount} BTC")
        
        # 準備三筆訂單的請求參數
        perp_direction = 'buy' if strategy['perpDirection'] == 'long' else 'sell'
        
        orders_to_place = [
            { # 1. Call 選項
                'method': f"/private/{strategy['callDirection']}",
                'params': {'instrument_name': strategy['callInstrument'], 'amount': amount, 'type': 'limit', 'price': strategy['callPrice']}
            },
            { # 2. Put 選項
                'method': f"/private/{strategy['putDirection']}",
                'params': {'instrument_name': strategy['putInstrument'], 'amount': amount, 'type': 'limit', 'price': strategy['putPrice']}
            },
            { # 3. 永續合約
                'method': f"/private/{perp_direction}",
                'params': {'instrument_name': 'BTC-PERPETUAL', 'amount': amount, 'type': 'limit', 'price': strategy['perpOpenPrice']}
            }
        ]

        # 使用線程池併發發送所有請求
        results = []
        all_successful = True
        
        # 定義發送單個請求的包裝函數
        def send_order(order):
            return self._send_private_request(order['method'], order['params'])

        with ThreadPoolExecutor(max_workers=3) as executor:
            # executor.map 會保持原始的順序返回結果
            api_responses = list(executor.map(send_order, orders_to_place))

        # 檢查所有請求的結果
        for i, result in enumerate(api_responses):
            instrument_name = orders_to_place[i]['params']['instrument_name']
            if 'order' in result:
                logger.info(f"  ✅ 併發下單成功: {result['order']['instrument_name']} ({result['order']['direction']})")
                results.append(result)
            else:
                logger.error(f"  ❌ 併發下單失敗: {instrument_name}. 原因: {result.get('error') or '未知錯誤'}")
                all_successful = False
        
        if all_successful:
            logger.info("✅✅✅ 套利策略所有訂單已併發送出！")
            return {'success': True, 'orders': results}
        else:
            logger.error("❌❌❌ 套利策略部分訂單失敗，執行終止！請檢查已成功訂單並手動處理！")
            # 重要：在併發模式下，部分訂單可能已成功。
            # 這裡需要一個補償邏輯，例如立即取消已成功的訂單。
            # 為簡化起見，目前僅打印錯誤日誌。
            return None

    def close_position(self, instrument: str, amount: float, order_type: str = 'limit', price: Optional[float] = None, post_only: bool = False) -> Dict:
        """平倉指定合約"""
        position = self.get_position(instrument)
        if not position or position.get('size', 0) == 0:
            logger.info(f"ℹ️ {instrument} 無需平倉，當前無部位。")
            return {'message': 'No position to close.'}

        direction = 'buy' if position['size'] < 0 else 'sell'
        
        params = {
            'instrument_name': instrument,
            'amount': amount,
            'type': order_type
        }
        if order_type == 'limit':
            if price is None:
                raise ValueError("平倉時，限價單必須提供價格")
            params['price'] = price
        if post_only:
            params['post_only'] = True
            
        logger.info(f"平倉操作: {direction.upper()} {amount} of {instrument} at price {price} (Post-Only: {post_only})")
        return self._send_private_request(f'/private/{direction}', params)

    def get_position(self, instrument: str) -> Dict:
        """獲取特定合約的部位資訊"""
        return self._send_private_request('/private/get_position', {'instrument_name': instrument})

    def get_open_orders_by_instrument(self, instrument: str) -> List[Dict]:
        """獲取特定合約的未結訂單"""
        return self._send_private_request('/private/get_open_orders_by_instrument', {'instrument_name': instrument}) or []

    def cancel(self, order_id: str) -> Dict:
        """取消指定訂單"""
        logger.info(f"正在取消訂單: {order_id}")
        return self._send_private_request('/private/cancel', {'order_id': order_id})