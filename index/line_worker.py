"""獨立執行 LINE 通知排程：python line_worker.py"""
import time
import os
import line_bot
from dotenv import load_dotenv
from supabase import create_client
from pathlib import Path
load_dotenv(Path(__file__).with_name('.env'))
if __name__ == '__main__':
    db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    if not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
        raise SystemExit('請先設定 LINE_CHANNEL_ACCESS_TOKEN')
    print('LINE 通知排程已啟動；按 Ctrl+C 停止。')
    while True:
        try:
            line_bot.run_jobs(db)
        except Exception as exc:
            print('排程失敗：', type(exc).__name__, '；請確認 SQL 與網路設定。')
        time.sleep(60)
