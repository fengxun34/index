"""離線測試：python -m unittest test_line_bot.py；不會發送 LINE 訊息。"""
import base64
import hashlib
import hmac
import importlib
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch

# 測試只需 Python 標準庫；HTTP 與 FastAPI 在此替換為測試介面。
if importlib.util.find_spec('fastapi') is None:
    fake = types.ModuleType('fastapi')
    fake.Header = lambda **kwargs: kwargs.get('default')
    fake.HTTPException = type('HTTPException', (Exception,), {})
    fake.Request = object
    sys.modules['fastapi'] = fake
if importlib.util.find_spec('httpx') is None:
    sys.modules['httpx'] = types.ModuleType('httpx')
import line_bot

class Query:
    def __init__(self, db, table):
        self.db, self.name, self.filters, self.change = db, table, {}, None
    def select(self, *_args): return self
    def eq(self, k, v): self.filters[k] = v; return self
    def update(self, change): self.change = change; return self
    def execute(self):
        rows = [r for r in self.db.rows[self.name] if all(r.get(k) == v for k,v in self.filters.items())]
        if self.change:
            for row in rows: row.update(self.change)
        return types.SimpleNamespace(data=rows)
class DB:
    def __init__(self, status='confirmed', enabled=True):
        self.rows = {
            'line_links': [{'patient_id':'p1','line_user_id':'U1','enabled':enabled}],
            'appointments': [{'id':'a1','doctor':'醫師','appointment_date':'2026-10-05','time_slot':'早上','status':status}],
            'line_outbox': [{'id':'job1','patient_id':'p1','event_key':'reminder:a1:2026-10-05:早上:醫師','message':'提醒','attempts':1}]}
    def table(self, name): return Query(self, name)
    def rpc(self, *_args): return types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=self.rows['line_outbox']))
class Tests(unittest.TestCase):
    def test_signature_tampering(self):
        body=b'{"events":[]}'
        sig=base64.b64encode(hmac.new(b'secret',body,hashlib.sha256).digest()).decode()
        self.assertTrue(line_bot.valid_signature(body,sig,'secret'))
        self.assertFalse(line_bot.valid_signature(body+b' ',sig,'secret'))
        self.assertFalse(line_bot.valid_signature(body,sig,''))
    def run_job(self, db, fails=False):
        with patch.object(line_bot,'line_request',side_effect=RuntimeError() if fails else None) as send:
            line_bot.run_jobs(db,datetime(2026,10,4,8))
            return send
    def test_cancelled_reminder_skipped(self):
        db=DB(status='cancelled');send=self.run_job(db)
        send.assert_not_called();self.assertEqual(db.rows['line_outbox'][0]['status'],'skipped')
    def test_unlinked_disabled_skipped(self):
        db=DB(enabled=False);self.run_job(db).assert_not_called()
    def test_changed_appointment_skipped(self):
        db=DB();db.rows['appointments'][0]['time_slot']='下午'
        self.run_job(db).assert_not_called()
    def test_send_marks_sent_and_uses_same_key(self):
        db=DB();send=self.run_job(db)
        self.assertEqual(send.call_args.args[2],'job1')
        self.assertEqual(db.rows['line_outbox'][0]['status'],'sent')
    def test_failure_returns_to_queue(self):
        db=DB();self.run_job(db,True)
        self.assertEqual(db.rows['line_outbox'][0]['status'],'pending')
        self.assertEqual(db.rows['line_outbox'][0]['last_error'],'RuntimeError')
if __name__=='__main__': unittest.main()
