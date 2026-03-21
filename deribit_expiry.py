# arbitrage_bot/deribit_expiry.py

"""
取得 Deribit 近期到期日資訊（帶快取）。
"""

import requests
import time
import logging
from datetime import datetime
from typing import Dict, Optional

from config import Config
from utils import CacheManager

logger = logging.getLogger(__name__)
_cache = CacheManager()


def get_tomorrow_expiry() -> Optional[Dict]:
    """
    取得明天到期的期權合約日期（帶快取）。
    回傳 {'dateStr', 'timestamp', 'fullDate'} 或 None。
    """
    cached = _cache.get('tomorrow_expiry', Config.EXPIRY_CACHE_SECONDS)
    if cached:
        return cached

    url    = f'{Config.DERIBIT_BASE_URL}/public/get_instruments'
    params = {'currency': 'BTC', 'kind': 'option', 'expired': 'false'}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'result' not in data:
            return None

        now               = int(time.time() * 1000)
        tomorrow          = now + 12 * 60 * 60 * 1000
        day_after_tomorrow= now + 48 * 60 * 60 * 1000

        expiry_map: Dict[str, Dict] = {}
        for instrument in data['result']:
            expiry_ts = instrument['expiration_timestamp']
            if tomorrow <= expiry_ts < day_after_tomorrow:
                parts    = instrument['instrument_name'].split('-')
                date_str = parts[1]
                if date_str not in expiry_map:
                    expiry_map[date_str] = {
                        'dateStr':   date_str,
                        'timestamp': expiry_ts,
                        'fullDate':  datetime.fromtimestamp(expiry_ts / 1000).strftime('%Y-%m-%d'),
                    }

        result = list(expiry_map.values())[0] if expiry_map else None
        if result:
            _cache.set('tomorrow_expiry', result)
        return result

    except requests.RequestException as e:
        logger.error(f'取得到期日失敗（網路）: {e}')
        return None
    except Exception as e:
        logger.error(f'處理到期日數據失敗: {e}')
        return None
