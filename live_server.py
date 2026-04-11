# arbitrage_bot/live_server.py

"""
FastAPI live server — serves the web dashboard and pushes real-time state
updates to connected browsers via WebSocket.
"""

import asyncio
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from bot_state import BotState
from config import Config
from notifications import send_verification_code, send_power_notification

logger = logging.getLogger(__name__)

app = FastAPI(title='Deribit Arb Dashboard', docs_url=None, redoc_url=None)

_clients: Set[WebSocket] = set()
_bot_state: Optional[BotState] = None
_server_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Verification state ────────────────────────────────────────────────────────
_pending_verification: Optional[Dict] = None
# { 'code': '123456', 'action': 'toggle_power'|'update_config',
#   'params': {...}, 'expires_at': float }

_VERIFY_TTL = 300  # 5 minutes


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get('/')
async def dashboard():
    return FileResponse(Path(__file__).parent / 'web' / 'index.html')


@app.get('/api/snapshot')
async def snapshot():
    if _bot_state:
        return JSONResponse(_bot_state.get_snapshot())
    return JSONResponse({'error': 'not ready'}, status_code=503)


@app.get('/api/config')
async def get_config():
    return JSONResponse(Config.get_public_config())


@app.post('/api/request-code')
async def request_code(request: Request):
    global _pending_verification
    body = await request.json()
    action = body.get('action', '')
    params = body.get('params', {})

    if action not in ('toggle_power', 'update_config'):
        return JSONResponse({'ok': False, 'message': '無效操作'}, status_code=400)

    # 修改參數只能在關機狀態
    if action == 'update_config' and _bot_state and _bot_state.trading_enabled:
        return JSONResponse({'ok': False, 'message': '請先關機再修改參數'}, status_code=400)

    code = ''.join(random.choices('0123456789', k=6))
    _pending_verification = {
        'code': code,
        'action': action,
        'params': params,
        'expires_at': time.time() + _VERIFY_TTL,
    }

    action_desc = '開關機切換' if action == 'toggle_power' else '修改策略參數'
    threading.Thread(
        target=send_verification_code, args=(code, action_desc), daemon=True
    ).start()

    return JSONResponse({'ok': True, 'message': '驗證碼已發送到 Telegram'})


@app.post('/api/verify-code')
async def verify_code(request: Request):
    global _pending_verification
    body = await request.json()
    code = body.get('code', '')

    if not _pending_verification:
        return JSONResponse({'ok': False, 'message': '無待驗證操作，請重新發送驗證碼'}, status_code=400)

    if time.time() > _pending_verification['expires_at']:
        _pending_verification = None
        return JSONResponse({'ok': False, 'message': '驗證碼已過期，請重新發送'}, status_code=400)

    if code != _pending_verification['code']:
        return JSONResponse({'ok': False, 'message': '驗證碼錯誤'}, status_code=400)

    action = _pending_verification['action']
    params = _pending_verification['params']
    _pending_verification = None

    result: Dict = {'action': action}

    if action == 'toggle_power' and _bot_state:
        # 開機時若有帶參數，先套用並持久化（關機時忽略 params）
        turning_on = not _bot_state.trading_enabled
        if turning_on and params:
            Config.update_config(params)
            _push_from_thread({'type': 'config_update', 'config': Config.get_public_config()})
            result['config'] = Config.get_public_config()

        new_state = turning_on
        _bot_state.update_trading_enabled(new_state)
        result['trading_enabled'] = new_state
        threading.Thread(target=send_power_notification, args=(new_state,), daemon=True).start()

    elif action == 'update_config':
        Config.update_config(params)
        result['config'] = Config.get_public_config()
        _push_from_thread({'type': 'config_update', 'config': Config.get_public_config()})

    return JSONResponse({'ok': True, **result})


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    logger.info(f'📺 Dashboard client connected ({len(_clients)} total)')
    try:
        if _bot_state:
            await ws.send_text(json.dumps(_bot_state.get_snapshot()))
        while True:
            await ws.receive_text()   # keep-alive pings from client
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f'WS client error: {e}')
    finally:
        _clients.discard(ws)
        logger.info(f'📺 Dashboard client disconnected ({len(_clients)} remaining)')


# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def _do_broadcast(msg: str) -> None:
    dead: Set[WebSocket] = set()
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _clients -= dead


def _push_from_thread(data: dict) -> None:
    """Thread-safe: schedule a broadcast on the server's event loop."""
    if _server_loop and not _server_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            _do_broadcast(json.dumps(data)), _server_loop
        )


# ── Server start ──────────────────────────────────────────────────────────────

def start_live_server(bot_state: BotState) -> None:
    """Start the FastAPI server in a background daemon thread."""
    global _bot_state
    _bot_state = bot_state
    bot_state.set_broadcast_callback(_push_from_thread)

    def run() -> None:
        global _server_loop
        _server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_server_loop)
        cfg = uvicorn.Config(
            app,
            host=Config.SERVER_HOST,
            port=Config.SERVER_PORT,
            loop='none',
            log_level='warning',
            access_log=False,
        )
        server = uvicorn.Server(cfg)
        _server_loop.run_until_complete(server.serve())

    t = threading.Thread(target=run, daemon=True, name='live-server')
    t.start()
    logger.info(f'🌐 Live server: http://{Config.SERVER_HOST}:{Config.SERVER_PORT}')
