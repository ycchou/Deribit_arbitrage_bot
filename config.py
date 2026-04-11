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
    TRADE_AMOUNT_BTC         = 0.3  # 單次交易單位
    MAX_CONCURRENT_POSITIONS = 8    # 同時最多持倉組數
    TRADE_COOLDOWN_SECONDS   = 60   # 組間冷卻（秒）

    # 掃描範圍：ATM 前後各幾檔 strike（1 = ±1，2 = ±2，4 = ±4）
    STRIKE_SCAN_RANGE        = 4

    # 到期日掃描窗口
    EXPIRY_MIN_HOURS         = 4    # 最短到期時間（小時）
    EXPIRY_MAX_HOURS         = 72   # 最長到期時間（小時）

    # ── 部位管理 ────────────────────────────────────────────────────────────────
    POSITION_CLOSE_TRIGGER_SECONDS = 60  # 到期前 60 秒觸發平倉
    TAKER_FORCE_CLOSE_SECONDS      = 10  # 到期前 10 秒強制 Taker 平倉

    # ── 掃描設定 ────────────────────────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS      = 1   # 主循環等待時間（秒），事件驅動後僅作兜底
    MIN_NET_PROFIT_OPPORTUNITY = 45  # 最低淨利潤門檻（美元）；包含市場衝擊/滑點緩衝

    # ── 風險控制 ─────────────────────────────────────────────────────────────────
    FAILURE_COOLDOWN_SECONDS    = 60   # 下單失敗後 1 分鐘冷卻，避免連續重試
    ENTRY_FILL_TIMEOUT_SECONDS  = 10   # 等待三條腿成交的最長秒數

    # ── Live Server ─────────────────────────────────────────────────────────────
    SERVER_HOST = _secrets.get("server", {}).get("host", "127.0.0.1")
    SERVER_PORT = int(_secrets.get("server", {}).get("port", 8080))

    @classmethod
    def get_public_config(cls) -> dict:
        """回傳可安全公開的設定參數（不含 API Key 等機密）。"""
        return {
            "strike_scan_range": cls.STRIKE_SCAN_RANGE,
            "expiry_min_hours": cls.EXPIRY_MIN_HOURS,
            "expiry_max_hours": cls.EXPIRY_MAX_HOURS,
            "scan_interval": cls.SCAN_INTERVAL_SECONDS,
            "trade_amount_btc": cls.TRADE_AMOUNT_BTC,
            "min_net_profit": cls.MIN_NET_PROFIT_OPPORTUNITY,
            "entry_fill_timeout": cls.ENTRY_FILL_TIMEOUT_SECONDS,
            "max_concurrent_positions": cls.MAX_CONCURRENT_POSITIONS,
            "trade_cooldown": cls.TRADE_COOLDOWN_SECONDS,
            "failure_cooldown": cls.FAILURE_COOLDOWN_SECONDS,
            "position_close_trigger": cls.POSITION_CLOSE_TRIGGER_SECONDS,
            "taker_force_close": cls.TAKER_FORCE_CLOSE_SECONDS,
            "use_testnet": cls.USE_TESTNET,
        }
