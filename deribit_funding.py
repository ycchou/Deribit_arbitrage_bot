# arbitrage_bot/deribit_funding.py

"""
取得 BTC-PERPETUAL 資金費率。
優先從 WebSocket ticker 取得，回退至快取，最後才呼叫 REST API。
"""

import requests
import logging
from typing import Optional, TYPE_CHECKING

from config import Config
from utils import CacheManager

if TYPE_CHECKING:
    from deribit_ws_client import DeribitWebSocket

logger = logging.getLogger(__name__)
_cache = CacheManager()

DEFAULT_FUNDING_RATE = 0.0001


def get_funding_rate(ws_client: Optional['DeribitWebSocket'] = None) -> float:
    """
    取得 8h 資金費率（優先 WebSocket → 快取 → REST）。
    """
    # 1. 從 WebSocket ticker 即時取得
    if ws_client:
        ticker = ws_client.get_ticker('BTC-PERPETUAL')
        if ticker and 'funding_8h' in ticker:
            rate = ticker['funding_8h']
            _cache.set('funding_rate', rate)
            logger.debug(f'✓ 從 WebSocket 取得資金費率: {rate * 100:.4f}%')
            return rate

    # 2. 使用快取
    cached = _cache.get('funding_rate', Config.FUNDING_RATE_CACHE_SECONDS)
    if cached is not None:
        logger.debug(f'✓ 使用快取資金費率: {cached * 100:.4f}%')
        return cached

    # 3. REST API 回退
    try:
        url    = f'{Config.DERIBIT_BASE_URL}/public/get_funding_rate_value'
        params = {'instrument_name': 'BTC-PERPETUAL'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'result' in data:
            rate = data['result']
            _cache.set('funding_rate', rate)
            logger.info(f'✓ 從 REST API 取得資金費率: {rate * 100:.4f}%')
            return rate

        logger.warning('⚠️ 無法從 API 取得資金費率，使用預設值')
        return DEFAULT_FUNDING_RATE

    except requests.RequestException as e:
        logger.warning(f'⚠️ 取得資金費率異常: {e}，使用預設值')
        return DEFAULT_FUNDING_RATE
