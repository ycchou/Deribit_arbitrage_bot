# arbitrage_bot/deribit_ws_client.py

"""
負責管理與 Deribit 的 WebSocket 連線、訂閱與數據接收。
"""
import asyncio
import websockets
import json
import time
import logging
import threading
from typing import Dict, List, Optional, Set

# 從 config 模組導入設定
from config import Config

logger = logging.getLogger(__name__)

class DeribitWebSocket:
    def __init__(self):
        self.ws = None
        self.loop = None
        self.thread = None
        self.is_running = False
        
        # 數據存儲（線程安全）
        self.ticker_data = {}
        self.data_lock = threading.Lock()
        self.last_update_time = {}
        
        # 訂閱管理
        self.subscribed_instruments = set()  # 已訂閱的合約
        self.pending_subscriptions = set()
        self.is_connected = False
        self.connection_ready = threading.Event()
        
        # 統計信息
        self.message_count = 0
        self.last_message_time = 0
        
    def start(self):
        """啟動 WebSocket 連接"""
        if self.is_running:
            logger.warning('WebSocket 已在運行中')
            return
        
        self.is_running = True
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()
        logger.info('✅ WebSocket 線程已啟動')
        
    def _run_event_loop(self):
        """在新線程中運行事件循環"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        while self.is_running:
            try:
                self.loop.run_until_complete(self._connect_and_run())
            except Exception as e:
                logger.error(f'❌ WebSocket 事件循環錯誤: {e}')
                self.connection_ready.clear()
                if self.is_running:
                    logger.info(f'⏳ {Config.WS_RECONNECT_DELAY} 秒後重新連接...')
                    time.sleep(Config.WS_RECONNECT_DELAY)

    async def _connect_and_run(self):
        """連接並運行 WebSocket"""
        try:
            async with websockets.connect(
                Config.DERIBIT_WS_URL,
                ping_interval=20,
                ping_timeout=10
            ) as websocket:
                self.ws = websocket
                self.is_connected = True
                logger.info('✅ WebSocket 已連接到 Deribit')
                
                # 重新訂閱之前的頻道
                if self.pending_subscriptions:
                    await self._resubscribe()
                
                self.connection_ready.set()
                
                # 啟動心跳和接收消息
                await asyncio.gather(
                    self._heartbeat(),
                    self._receive_messages()
                )
        except websockets.exceptions.ConnectionClosed:
            logger.warning('⚠️ WebSocket 連接已關閉')
            self.is_connected = False
            self.connection_ready.clear()
        except Exception as e:
            logger.error(f'❌ WebSocket 連接錯誤: {e}')
            self.is_connected = False
            self.connection_ready.clear()

    async def _heartbeat(self):
        """發送心跳以保持連接"""
        while self.is_connected:
            try:
                await asyncio.sleep(Config.WS_HEARTBEAT_INTERVAL)
                if self.ws:
                    await self.ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": int(time.time() * 1000),
                        "method": "public/test"
                    }))
            except Exception as e:
                logger.error(f'❌ 心跳發送失敗: {e}')
                break
    
    async def _receive_messages(self):
        """接收並處理 WebSocket 消息"""
        try:
            async for message in self.ws:
                data = json.loads(message)
                await self._handle_message(data)
                self.message_count += 1
                self.last_message_time = time.time()
        except Exception as e:
            logger.error(f'❌ 接收消息時發生錯誤: {e}')
    
    async def _handle_message(self, data: dict):
        """處理接收到的消息"""
        try:
            # 訂閱成功回應
            if 'result' in data and isinstance(data.get('result'), list):
                channels = data['result']
                logger.info(f'✅ 訂閱成功: {len(channels)} 個頻道')
                return
            
            # 訂閱錯誤
            if 'error' in data:
                logger.error(f'❌ WebSocket 錯誤: {data["error"]}')
                return
            
            # Ticker 數據更新
            if 'params' in data and 'data' in data['params']:
                channel = data['params'].get('channel', '')
                if channel.startswith('ticker.'):
                    ticker_data = data['params']['data']
                    instrument_name = ticker_data.get('instrument_name')
                    
                    if instrument_name:
                        with self.data_lock:
                            is_new = instrument_name not in self.ticker_data
                            self.ticker_data[instrument_name] = ticker_data
                            self.last_update_time[instrument_name] = time.time()
                        
                        if is_new:
                            logger.info(f'📊 首次接收 {instrument_name} 數據')
                            # 如果是永續合約，顯示資金費率
                            if instrument_name == 'BTC-PERPETUAL' and 'funding_8h' in ticker_data:
                                funding_rate = ticker_data['funding_8h']
                                logger.info(f'    資金費率 (8H): {funding_rate * 100:.4f}%')
        except Exception as e:
            logger.error(f'❌ 處理消息時發生錯誤: {e}')
    
    async def _resubscribe(self):
        """重新訂閱所有頻道"""
        if self.pending_subscriptions:
            channels = list(self.pending_subscriptions)
            await self._subscribe_channels(channels)
            logger.info(f'✅ 已重新訂閱 {len(channels)} 個頻道')

    async def _subscribe_channels(self, channels: List[str]):
        """訂閱頻道"""
        if not self.ws or not self.is_connected:
            logger.warning('⚠️ WebSocket 未連接，無法訂閱')
            return False
        
        try:
            message = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000),
                "method": "public/subscribe",
                "params": {
                    "channels": channels
                }
            }
            
            await self.ws.send(json.dumps(message))
            self.pending_subscriptions.update(channels)
            
            return True
        except Exception as e:
            logger.error(f'❌ 訂閱頻道失敗: {e}')
            return False

    def subscribe_instruments(self, instruments: List[str]) -> bool:
        """訂閱合約（智能訂閱，只訂閱新合約）"""
        new_instruments = [inst for inst in instruments if inst not in self.subscribed_instruments]
        
        if not new_instruments:
            logger.debug(f'✓ 所有合約已訂閱，無需重複訂閱')
            return True
        
        logger.info(f'📡 訂閱 {len(new_instruments)} 個新合約（總共 {len(instruments)} 個）')
        
        channels = [f'ticker.{instrument}.100ms' for instrument in new_instruments]
        
        if self.is_connected and self.loop:
            future = asyncio.run_coroutine_threadsafe(
                self._subscribe_channels(channels),
                self.loop
            )
            try:
                result = future.result(timeout=5)
                if result:
                    self.subscribed_instruments.update(new_instruments)
                    logger.info(f'✅ 成功訂閱新合約，當前已訂閱 {len(self.subscribed_instruments)} 個合約')
                return result
            except Exception as e:
                logger.error(f'❌ 訂閱執行失敗: {e}')
                return False
        else:
            self.pending_subscriptions.update(channels)
            logger.info(f'⏳ WebSocket 未連接，已加入待處理列表')
            return False

    def get_ticker(self, instrument_name: str) -> Optional[Dict]:
        """獲取 Ticker 數據"""
        with self.data_lock:
            return self.ticker_data.get(instrument_name)

    def is_data_ready(self, instruments: List[str]) -> bool:
        """檢查所有需要的合約數據是否已就緒"""
        with self.data_lock:
            for instrument in instruments:
                if instrument not in self.ticker_data:
                    return False
                # 檢查數據是否太舊（超過10秒）
                last_update = self.last_update_time.get(instrument, 0)
                if time.time() - last_update > 10:
                    return False
        return True

    def get_statistics(self) -> Dict:
        """獲取統計信息"""
        with self.data_lock:
            return {
                'connected': self.is_connected,
                'subscribed_instruments': len(self.subscribed_instruments),
                'instruments_with_data': len(self.ticker_data),
                'message_count': self.message_count,
                'last_message_age': time.time() - self.last_message_time if self.last_message_time > 0 else -1
            }

    def wait_for_connection(self, timeout: float = 10) -> bool:
        """等待 WebSocket 連接就緒"""
        return self.connection_ready.wait(timeout=timeout)

    def wait_for_data(self, instruments: List[str], timeout: float = 10) -> bool:
        """等待特定合約數據就緒"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_data_ready(instruments):
                return True
            time.sleep(0.2)
        return False
        
    def stop(self):
        """停止 WebSocket 連接"""
        logger.info('🛑 正在停止 WebSocket...')
        self.is_running = False
        self.is_connected = False
        self.connection_ready.clear()
        
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        if self.thread:
            self.thread.join(timeout=5)
        
        logger.info('✅ WebSocket 已停止')