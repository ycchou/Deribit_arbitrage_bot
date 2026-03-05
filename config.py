# arbitrage_bot/config.py

"""
存放所有靜態設定值。
機密資料（API Key 等）從同目錄的 secrets.yaml 讀取。
"""

import os
import yaml
from pathlib import Path


def _load_secrets() -> dict:
    secrets_path = Path(__file__).parent / "secrets.yaml"
    if not secrets_path.exists():
        raise FileNotFoundError(
            f"找不到 secrets.yaml，請複製 secrets.example.yaml 並填入真實資料。\n"
            f"路徑: {secrets_path}"
        )
    with open(secrets_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_secrets = _load_secrets()


class Config:
    # ── 機密資料（從 secrets.yaml 讀取）────────────────────────────────────────
    DERIBIT_CLIENT_ID     = _secrets["deribit"]["client_id"]
    DERIBIT_CLIENT_SECRET = _secrets["deribit"]["client_secret"]
    TELEGRAM_BOT_TOKEN    = _secrets["telegram"]["bot_token"]
    TELEGRAM_CHAT_ID      = _secrets["telegram"]["chat_id"]

    # ── 環境切換 ────────────────────────────────────────────────────────────────
    USE_TESTNET = True   # True = testnet，False = 正式網

    # ── Deribit API 端點（自動依 USE_TESTNET 切換）──────────────────────────────
    DERIBIT_BASE_URL = (
        'https://test.deribit.com/api/v2' if USE_TESTNET
        else 'https://www.deribit.com/api/v2'
    )
    DERIBIT_WS_URL = (
        'wss://test.deribit.com/ws/api/v2' if USE_TESTNET
        else 'wss://www.deribit.com/ws/api/v2'
    )

    TELEGRAM_BASE_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'

    # ── WebSocket 設定 ──────────────────────────────────────────────────────────
    WS_HEARTBEAT_INTERVAL = 10
    WS_RECONNECT_DELAY    = 5

    # ── 緩存設定 ────────────────────────────────────────────────────────────────
    FUNDING_RATE_CACHE_SECONDS = 3600  # 資金費率緩存 60 分鐘
    EXPIRY_CACHE_SECONDS       = 3600  # 到期日緩存 1 小時

    # ── 交易與風險控制 ──────────────────────────────────────────────────────────
    TRADE_AMOUNT_BTC         = 0.3          # 單次交易單位
    COOLDOWN_PERIOD_SECONDS  = 28 * 60 * 60 # 28 小時冷卻期

    # ── 部位管理 ────────────────────────────────────────────────────────────────
    POSITION_CLOSE_TRIGGER_SECONDS = 60  # 到期前 60 秒觸發平倉
    TAKER_FORCE_CLOSE_SECONDS      = 10  # 到期前 10 秒強制 Taker 平倉

    # ── 掃描設定 ────────────────────────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS      = 1   # 主循環等待時間（秒），事件驅動後僅作兜底
    MIN_NET_PROFIT_OPPORTUNITY = 20  # 最低淨利潤門檻（美元）
