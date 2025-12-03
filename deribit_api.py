# arbitrage_bot/deribit_api.py

"""
存放所有與 Deribit REST API 互動的函式。
包含獲取合約、履約價、資金費率等功能，並內建緩存機制。
"""
import requests
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING

from config import Config
from utils import CacheManager

# 為了避免循環導入 (circular import)，僅在類型檢查時導入 DeribitWebSocket
if TYPE_CHECKING:
    from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)

# 實例化一個此模組專用的緩存管理器
cache_manager = CacheManager()

def get_tomorrow_expiry() -> Optional[Dict]:
    """獲取明天到期的期權合約日期（帶緩存）"""
    cached = cache_manager.get('tomorrow_expiry', Config.EXPIRY_CACHE_SECONDS)
    if cached:
        return cached
    
    url = f'{Config.DERIBIT_BASE_URL}/public/get_instruments'
    params = {'currency': 'BTC', 'kind': 'option', 'expired': 'false'}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'result' not in data:
            return None
        
        now = int(time.time() * 1000)
        tomorrow = now + 12 * 60 * 60 * 1000
        day_after_tomorrow = now + 48 * 60 * 60 * 1000
        
        expiry_map = {}
        for instrument in data['result']:
            expiry_timestamp = instrument['expiration_timestamp']
            
            if tomorrow <= expiry_timestamp < day_after_tomorrow:
                parts = instrument['instrument_name'].split('-')
                date_str = parts[1]
                
                if date_str not in expiry_map:
                    expiry_map[date_str] = {
                        'dateStr': date_str,
                        'timestamp': expiry_timestamp,
                        'fullDate': datetime.fromtimestamp(expiry_timestamp / 1000).strftime('%Y-%m-%d')
                    }
        
        result = list(expiry_map.values())[0] if expiry_map else None
        
        if result:
            cache_manager.set('tomorrow_expiry', result)
        
        return result
    except requests.RequestException as e:
        logger.error(f'獲取到期日失敗 (網路問題): {e}')
        return None
    except Exception as e:
        logger.error(f'處理到期日數據失敗: {e}')
        return None

def get_target_strikes(perpetual_price: float, expiry_date_str: str) -> List[float]:
    """獲取指定到期日、永續價格上下各一檔的履約價（帶緩存）"""
    price_range = int(perpetual_price / 1000) * 1000
    cache_key = f'strikes_{expiry_date_str}_{price_range}'
    
    cached = cache_manager.get(cache_key, 300)
    if cached:
        sorted_strikes = cached
        closest_index = min(range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - perpetual_price))
        start_index = max(0, closest_index - 1)
        end_index = min(len(sorted_strikes), closest_index + 2)
        return sorted_strikes[start_index:end_index]
    
    url = f'{Config.DERIBIT_BASE_URL}/public/get_instruments'
    params = {'currency': 'BTC', 'kind': 'option', 'expired': 'false'}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'result' not in data:
            return []
        
        strikes = set()
        for instrument in data['result']:
            if f'-{expiry_date_str}-' in instrument['instrument_name']:
                parts = instrument['instrument_name'].split('-')
                try:
                    strike = float(parts[2])
                    strikes.add(strike)
                except (ValueError, IndexError):
                    continue
        
        sorted_strikes = sorted(list(strikes))
        
        cache_manager.set(cache_key, sorted_strikes)
        
        if not sorted_strikes:
            return []

        closest_index = min(range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - perpetual_price))
        start_index = max(0, closest_index - 1)
        end_index = min(len(sorted_strikes), closest_index + 2)
        
        return sorted_strikes[start_index:end_index]
    except requests.RequestException as e:
        logger.error(f'獲取履約價失敗 (網路問題): {e}')
        return []
    except Exception as e:
        logger.error(f'處理履約價數據失敗: {e}')
        return []

def get_funding_rate(ws_client: Optional['DeribitWebSocket'] = None) -> float:
    """獲取資金費率（優先使用 WebSocket，帶緩存）"""
    if ws_client:
        perpetual_ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if perpetual_ticker and 'funding_8h' in perpetual_ticker:
            funding_rate = perpetual_ticker['funding_8h']
            cache_manager.set('funding_rate', funding_rate)
            logger.debug(f'✓ 從 WebSocket 獲取資金費率: {funding_rate * 100:.4f}%')
            return funding_rate

    cached = cache_manager.get('funding_rate', Config.FUNDING_RATE_CACHE_SECONDS)
    if cached is not None:
        logger.debug(f'✓ 使用緩存的資金費率: {cached * 100:.4f}%')
        return cached

    try:
        url = f'{Config.DERIBIT_BASE_URL}/public/get_funding_rate_value'
        params = {'instrument_name': 'BTC-PERPETUAL'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'result' in data:
            rate = data['result']
            cache_manager.set('funding_rate', rate)
            logger.info(f'✓ 從 REST API 獲取資金費率: {rate * 100:.4f}%')
            return rate
        
        logger.warning(f'⚠️ 無法從 API 獲取資金費率，使用默認值')
        return 0.0001
    except requests.RequestException as e:
        logger.warning(f'⚠️ 獲取資金費率異常: {e}，使用默認值')
        return 0.0001