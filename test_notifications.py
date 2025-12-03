# test_notifications.py

"""
一個專門用來測試 Telegram 通知功能的獨立腳本。
"""
import logging
from notifications import send_trade_execution_notification, send_liquidity_issue_notification
from utils import setup_logging

# 初始化日誌，這樣我們可以看到 notifications.py 中的 log 訊息
setup_logging()
logger = logging.getLogger(__name__)

def create_fake_opportunity() -> dict:
    """建立一個假的套利機會物件，用於測試。"""
    return {
        'strategyName': '測試策略 (買Call+賣Put)',
        'strike': 65000.0,
        'expiryDate': '30OCT25', # 假設的日期
        'expiryFullDate': '2025-10-30',
        'netProfit': 123.45,
        'grossProfit': 150.00,
        'callInstrument': 'BTC-30OCT25-65000-C',
        'putInstrument': 'BTC-30OCT25-65000-P',
        # 用於流動性不足通知的額外欄位
        'callAmount': 10.5,
        'putAmount': 0.8, # <--- 故意設定一個不足的值
        'perpAmount': 25.0,
    }

if __name__ == '__main__':
    logger.info("🤖 開始測試 Telegram 通知功能...")
    
    fake_op = create_fake_opportunity()
    
    # --- 測試 1: 交易成功執行的通知 ---
    logger.info("\n--- 測試 1: 發送「交易成功」通知 ---")
    print("正在發送「交易成功」通知，請檢查您的 Telegram...")
    success1 = send_trade_execution_notification(fake_op)
    if success1:
        print("✅ 通知 1 發送成功！")
    else:
        print("❌ 通知 1 發送失敗，請檢查日誌與 config.py 設定。")

    # --- 測試 2: 流動性不足的通知 ---
    logger.info("\n--- 測試 2: 發送「流動性不足」通知 ---")
    print("正在發送「流動性不足」通知，請檢查您的 Telegram...")
    success2 = send_liquidity_issue_notification(fake_op)
    if success2:
        print("✅ 通知 2 發送成功！")
    else:
        print("❌ 通知 2 發送失敗，請檢查日誌與 config.py 設定。")

    logger.info("\n✅ 所有測試完成。")