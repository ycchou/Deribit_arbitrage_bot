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


def _slice_strikes(sorted_strikes: List[float], price: float) -> List[float]:
    """依 STRIKE_SCAN_RANGE 截取 ATM 附近的履約價子集。"""
    closest_idx = min(
        range(len(sorted_strikes)),
        key=lambda i: abs(sorted_strikes[i] - price),
    )
    r     = Config.STRIKE_SCAN_RANGE
    start = max(0, closest_idx - r)
    end   = min(len(sorted_strikes), closest_idx + r + 1)
    return sorted_strikes[start:end]


def get_target_strikes(perpetual_price: float, expiry_date_str: str) -> List[float]:
    """
    取得指定到期日、靠近永續價格前後各 STRIKE_SCAN_RANGE 檔的履約價。
    快取 key 以 $2000 為步長，降低 BTC 在整千位震盪時的 REST 觸發頻率。
    """
    # $2000 步長：比 $1000 更穩定，減少 BTC 在整千位附近抖動時的 cache miss
    price_bucket = int(perpetual_price / 2000) * 2000
    cache_key    = f'strikes_{expiry_date_str}_{price_bucket}'

    cached = _cache.get(cache_key, 300)
    if cached:
        return _slice_strikes(cached, perpetual_price)

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

        return _slice_strikes(sorted_strikes, perpetual_price)

    except requests.RequestException as e:
        logger.error(f'取得履約價失敗（網路）: {e}')
        return []
    except Exception as e:
        logger.error(f'處理履約價數據失敗: {e}')
        return []
