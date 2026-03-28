# arbitrage_bot/deribit_api.py

"""
向後相容匯出層。
各功能已拆分至獨立模組：
  - deribit_expiry.py   → get_tomorrow_expiry
  - deribit_strikes.py  → get_target_strikes
  - deribit_funding.py  → get_funding_rate
"""

from deribit_expiry  import get_tomorrow_expiry, get_available_expiries
from deribit_strikes import get_target_strikes
from deribit_funding import get_funding_rate

__all__ = ['get_tomorrow_expiry', 'get_available_expiries', 'get_target_strikes', 'get_funding_rate']
