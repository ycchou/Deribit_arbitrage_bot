# arbitrage_bot/config.py

"""
存放所有靜態設定值。
機密資料（API Key 等）從同目錄的 secrets.yaml 讀取。
"""

import json
import os
import yaml
from pathlib import Path
from typing import Any, Dict


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
    USE_TESTNET = False   # True = testnet，False = 正式網

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
    WS_RECONNECT_DELAY    = 1

    # ── 緩存設定 ────────────────────────────────────────────────────────────────
    FUNDING_RATE_CACHE_SECONDS = 3600  # 資金費率緩存 60 分鐘
    EXPIRY_CACHE_SECONDS       = 3600  # 到期日緩存 1 小時

    # ── 交易與風險控制 ──────────────────────────────────────────────────────────
    TRADE_AMOUNT_BTC         = 0.3  # 單次交易單位
    MAX_CONCURRENT_POSITIONS = 8    # 同時最多持倉組數
    TRADE_COOLDOWN_SECONDS   = 30   # 組間冷卻（秒）

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
    MIN_NET_PROFIT_OPPORTUNITY = 35  # 最低淨利潤門檻（美元）；包含市場衝擊/滑點緩衝

    # ── 風險控制 ─────────────────────────────────────────────────────────────────
    FAILURE_COOLDOWN_SECONDS    = 60   # 下單失敗後 1 分鐘冷卻，避免連續重試
    CONNECTION_FAILURE_COOLDOWN_SECONDS = 5   # WS 連線問題冷卻（短，重連後快速恢復）
    PRETRADE_PING_ENABLED       = True  # 下單前 WS 健康預檢；關閉可減少延遲，但風險自負
    ENTRY_FILL_TIMEOUT_SECONDS  = 10   # 等待三條腿成交的最長秒數

    # ── Live Server ─────────────────────────────────────────────────────────────
    SERVER_HOST = _secrets.get("server", {}).get("host", "127.0.0.1")
    SERVER_PORT = int(_secrets.get("server", {}).get("port", 8080))

    # ── Runtime config persistence ────────────────────────────────────────────
    RUNTIME_CONFIG_PATH = Path(__file__).parent / 'runtime_config.json'

    # 可由 Dashboard 修改的參數白名單：key → (Config attr name, type cast)
    _ALLOWED_PARAMS: Dict[str, tuple] = {
        'trade_amount_btc':        ('TRADE_AMOUNT_BTC',              float),
        'min_net_profit':          ('MIN_NET_PROFIT_OPPORTUNITY',    float),
        'strike_scan_range':       ('STRIKE_SCAN_RANGE',            int),
        'expiry_min_hours':        ('EXPIRY_MIN_HOURS',             int),
        'expiry_max_hours':        ('EXPIRY_MAX_HOURS',             int),
        'max_concurrent_positions':('MAX_CONCURRENT_POSITIONS',     int),
        'trade_cooldown':          ('TRADE_COOLDOWN_SECONDS',       int),
        'failure_cooldown':        ('FAILURE_COOLDOWN_SECONDS',     int),
        'entry_fill_timeout':      ('ENTRY_FILL_TIMEOUT_SECONDS',   int),
        'position_close_trigger':  ('POSITION_CLOSE_TRIGGER_SECONDS', int),
        'taker_force_close':       ('TAKER_FORCE_CLOSE_SECONDS',    int),
        'scan_interval':           ('SCAN_INTERVAL_SECONDS',        int),
        'pretrade_ping_enabled':   ('PRETRADE_PING_ENABLED',        lambda v: v if isinstance(v, bool) else str(v).lower() == 'true'),
    }

    @classmethod
    def update_config(cls, params: Dict[str, Any], persist: bool = True) -> None:
        """更新記憶體中的 Config 值，可選持久化到 runtime_config.json。"""
        for key, val in params.items():
            if key in cls._ALLOWED_PARAMS:
                attr, expected_type = cls._ALLOWED_PARAMS[key]
                setattr(cls, attr, expected_type(val))

        if persist:
            existing: dict = {}
            if cls.RUNTIME_CONFIG_PATH.exists():
                existing = json.loads(cls.RUNTIME_CONFIG_PATH.read_text(encoding='utf-8'))
            existing.update(params)
            cls.RUNTIME_CONFIG_PATH.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False), encoding='utf-8'
            )

    @classmethod
    def load_runtime_overrides(cls) -> None:
        """啟動時載入 runtime_config.json 覆蓋預設值（不重寫檔案）。"""
        if cls.RUNTIME_CONFIG_PATH.exists():
            overrides = json.loads(cls.RUNTIME_CONFIG_PATH.read_text(encoding='utf-8'))
            cls.update_config(overrides, persist=False)

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
            "pretrade_ping_enabled": cls.PRETRADE_PING_ENABLED,
        }
