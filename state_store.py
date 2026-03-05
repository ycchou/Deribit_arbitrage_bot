# arbitrage_bot/state_store.py

"""
簡易 JSON 狀態持久化（Fix #10）。
儲存重啟後需要恢復的關鍵狀態：last_trade_time、active_position。
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)
_STATE_FILE = Path(__file__).parent / 'state.json'


def load() -> Dict[str, Any]:
    if not _STATE_FILE.exists():
        return {}
    try:
        with open(_STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f'⚠️ 讀取 state.json 失敗: {e}，使用空狀態')
        return {}


def save(key: str, value: Any) -> None:
    state = load()
    if value is None:
        state.pop(key, None)
    else:
        state[key] = value
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f'❌ 儲存 state.json 失敗: {e}')
