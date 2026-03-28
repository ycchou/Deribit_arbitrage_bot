# arbitrage_bot/deribit_expiry.py

"""
取得 Deribit 近期到期日資訊（帶快取）。
"""

import requests
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional

from config import Config
from utils import CacheManager

logger = logging.getLogger(__name__)
_cache = CacheManager()


def get_available_expiries() -> List[Dict]:
    """
    取得在 [+EXPIRY_MIN_HOURS, +EXPIRY_MAX_HOURS) 窗口內所有可用到期日。
    回傳 List[{'dateStr', 'timestamp', 'fullDate'}]，按到期時間升序排列。
    結果快取 1 小時。
    """
    cached = _cache.get('available_expiries', Config.EXPIRY_CACHE_SECONDS)
    if cached:
        return cached

    url    = f'{Config.DERIBIT_BASE_URL}/public/get_instruments'
    params = {'currency': 'BTC', 'kind': 'option', 'expired': 'false'}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'result' not in data:
            return []

        now      = int(time.time() * 1000)
        min_ts   = now + Config.EXPIRY_MIN_HOURS * 3600 * 1000
        max_ts   = now + Config.EXPIRY_MAX_HOURS * 3600 * 1000

        expiry_map: Dict[str, Dict] = {}
        for instrument in data['result']:
            expiry_ts = instrument['expiration_timestamp']
            if min_ts <= expiry_ts < max_ts:
                parts    = instrument['instrument_name'].split('-')
                date_str = parts[1]
                if date_str not in expiry_map:
                    expiry_map[date_str] = {
                        'dateStr':   date_str,
                        'timestamp': expiry_ts,
                        'fullDate':  datetime.fromtimestamp(expiry_ts / 1000).strftime('%Y-%m-%d'),
                    }

        result = sorted(expiry_map.values(), key=lambda x: x['timestamp'])
        if result:
            _cache.set('available_expiries', result)
        return result

    except requests.RequestException as e:
        logger.error(f'取得到期日失敗（網路）: {e}')
        return []
    except Exception as e:
        logger.error(f'處理到期日數據失敗: {e}')
        return []


def get_tomorrow_expiry() -> Optional[Dict]:
    """向後相容 wrapper：回傳最近一個可用到期日。"""
    expiries = get_available_expiries()
    return expiries[0] if expiries else None
