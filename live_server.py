# arbitrage_bot/live_server.py

"""
FastAPI live server — serves the web dashboard and pushes real-time state
updates to connected browsers via WebSocket.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Set, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from bot_state import BotState
from config import Config

logger = logging.getLogger(__name__)

app = FastAPI(title='Deribit Arb Dashboard', docs_url=None, redoc_url=None)

_clients: Set[WebSocket] = set()
_bot_state: Optional[BotState] = None
_server_loop: Optional[asyncio.AbstractEventLoop] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get('/')
async def dashboard():
    return FileResponse(Path(__file__).parent / 'web' / 'index.html')


@app.get('/api/snapshot')
async def snapshot():
    if _bot_state:
        return JSONResponse(_bot_state.get_snapshot())
    return JSONResponse({'error': 'not ready'}, status_code=503)


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
