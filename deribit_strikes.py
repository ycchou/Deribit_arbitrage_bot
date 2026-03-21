# arbitrage_bot/deribit_strikes.py

"""
取得 Deribit 指定到期日附近的履約價列表（帶快取）。
"""

import requests
import logging
from typing import List

from config import Config
from utils import CacheManager

logger = logging.getLogger(__name__)
_cache = CacheManager()


def get_target_strikes(perpetual_price: float, expiry_date_str: str) -> List[float]:
    """
    取得指定到期日、靠近永續價格前後各一檔的履約價（帶快取）。
    回傳排序後的 float 列表。
    """
    price_range = int(perpetual_price / 1000) * 1000
    cache_key   = f'strikes_{expiry_date_str}_{price_range}'

    cached = _cache.get(cache_key, 300)
    if cached:
        sorted_strikes = cached
        closest_idx = min(range(len(sorted_strikes)),
                          key=lambda i: abs(sorted_strikes[i] - perpetual_price))
        start = max(0, closest_idx - 1)
        end   = min(len(sorted_strikes), closest_idx + 2)
        return sorted_strikes[start:end]

    url    = f'{Config.DERIBIT_BASE_URL}/public/get_instruments'
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
                    strikes.add(float(parts[2]))
                except (ValueError, IndexError):
                    continue

        sorted_strikes = sorted(strikes)
        _cache.set(cache_key, sorted_strikes)

        if not sorted_strikes:
            return []

        closest_idx = min(range(len(sorted_strikes)),
                          key=lambda i: abs(sorted_strikes[i] - perpetual_price))
        start = max(0, closest_idx - 1)
        end   = min(len(sorted_strikes), closest_idx + 2)
        return sorted_strikes[start:end]

    except requests.RequestException as e:
        logger.error(f'取得履約價失敗（網路）: {e}')
        return []
    except Exception as e:
        logger.error(f'處理履約價數據失敗: {e}')
        return []
