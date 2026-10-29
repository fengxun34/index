"""離線測試，不呼叫真實 Supabase、LINE 或 OpenAI。"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import date
from pathlib import Path

os.environ.pop('SUPABASE_URL', None)
os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
os.environ.pop('OPENAI_API_KEY', None)
with patch("dotenv.load_dotenv", return_value=False):
    import app
import agent
from clinical_demo import QUESTIONS, assess, summarize, next_choice
from fastapi.testclient import TestClient

NORMAL = {'emergency_screen': '以上都沒有', 'urgent_screen': '以上都沒有',
          'duration': '一週內', 'onset': '搬重物或運動後', 'pain': '右腰痠痛 4 分，工作稍受影響',
          'radiate': '沒有延伸，也沒有麻木無力', 'risk_history': '以上都沒有', 'trigger': '久坐較痛，沒有用藥'}

class Query:
    def __init__(self, rows):
        self.rows = list(rows)
    def select(self, *args): return self
    def eq(self, k, v):
        self.rows = [r for r in self.rows if r.get(k) == v]; return self
    def in_(self, k, vs):
        self.rows = [r for r in self.rows if r.get(k) in vs]; return self
    def execute(self): return SimpleNamespace(data=self.rows)

class Database:
    def __init__(self, rows): self.rows = rows
    def table(self, name): return Query(self.rows.get(name, []))

class ClinicalTests(unittest.TestCase):
    def test_screening_and_negation(self):
        self.assertEqual(assess(NORMAL)['level'], 'routine')
        self.assertEqual(assess({**NORMAL, 'pain': '沒有大小便失禁，也沒有發燒'})['level'], 'routine')
        self.assertEqual(assess({**NORMAL, 'pain': '沒有失禁，但現在尿不出來'})['level'], 'emergency')
        self.assertEqual(assess({**NORMAL, 'mainComplaint': '腰痛尿不出來'})['level'], 'emergency')
        self.assertEqual(assess({**NORMAL, 'urgent_screen': '有上述至少一項'})['level'], 'urgent')
        self.assertEqual(assess({**NORMAL, 'neuro_detail': '右腿越來越無力'})['level'], 'urgent')
        self.assertEqual(assess({**NORMAL, 'risk_history': '有癌症病史'})['level'], 'review')

    def test_unknown_is_not_negative(self):
        for value in ('不確定', '（略過）', '', '沒有失禁但其他不知道'):
            a = {**NORMAL, 'emergency_screen': value}
            self.assertEqual(assess(a)['level'], 'unconfirmed')
            self.assertTrue(summarize('腰椎', a)['missing'])

    def test_branch_and_no_early_exit(self):
        self.assertEqual(next_choice({'emergency_screen': '以上都沒有'}, QUESTIONS[1:])['key'], 'urgent_screen')
        left = [q for q in QUESTIONS if q['key'] in ('neuro_detail', 'risk_history')]
        self.assertEqual(next_choice(NORMAL, left)['key'], 'risk_history')
        self.assertEqual(next_choice({**NORMAL, 'radiate': '延伸到臀部或單腿'}, left)['key'], 'neuro_detail')
        self.assertEqual(next_choice({**NORMAL, 'radiate': '不確定'}, left)['key'], 'neuro_detail')

    def test_backend_questions_rag_and_booking_guard(self):
        with patch.object(app, 'supabase', None):
            app.rebuild_rag_index()
            self.assertEqual(len(app.contents), 60)
            result = app.rag_answer(app.AnswerRequest(question='腰痛影像檢查 MRI X光', metadata={'body_part': '腰椎'}, explain=False))
            self.assertTrue(result['retrieved_chunks'])
            self.assertIn('待診所醫師確認', result['answer'])
            self.assertTrue(any('https://' in h['content'] for h in result['retrieved_chunks']))
            with TestClient(app.app) as client:
                qs = client.get('/api/clinic/questions').json()['questions']['腰椎']
                self.assertEqual(qs[0]['key'], 'emergency_screen')
                r = client.post('/api/triage/next-question', json={'body_part':'腰椎','answers': {'emergency_screen':'有上述至少一項'},'remaining_keys':['duration']}).json()
                self.assertEqual(r['action'], 'alert')
                booking = {'name':'測試病患', 'id_number':'A123456789','department':'脊椎外科','doctor':'高醫師', 'slot':'2026-12-14 早上 09:00 - 12:00','body_part':'腰椎','qa_answers': {'emergency_screen':'有上述至少一項'}}
                r = client.post('/api/booking/confirm', json=booking).json()
                self.assertEqual(r['status'], 'error')
                self.assertIn('急診', r['message'])
                with patch.object(app.agent_llm, 'llm_enabled', return_value=True), patch.object(app.agent_llm, 'handle_turn_llm', side_effect=AssertionError('腰痛不應由模型跳過必問題目')):
                    r = client.post('/api/agent/chat', json={'message':'我腰痛'}).json()
                    self.assertIn('排尿', r['reply'])
                    self.assertTrue(r['state']['clinical_demo'])

    def test_rule_chat_complete_and_skip(self):
        tools = agent.AgentTools(questions=lambda part: QUESTIONS, next_available=lambda *args: {'status':'error', 'message':'測試無時段'},
          clinic_schedule=lambda *args: {}, book=lambda *args: self.fail('不應寫入'), history=lambda *args: {}, cancel=lambda *args: {},
          reschedule=lambda *args: {}, rag=lambda *args: {'answer':'示範'}, doctors={}, category_labels={'脊椎外科':'脊椎'}, referral_categories={})
        r = agent.handle_turn(None, '我腰痛', tools, today=date(2026,10,12))
        self.assertIn('排尿', r['reply'])
        asked = []
        for _ in range(12):
            state = r['state']
            if state['step'] != 'qa': break
            key = state.get('clinical_question_key', 'emergency_screen')
            asked.append(key)
            r = agent.handle_turn(state, NORMAL.get(key, '（略過）'), tools, today=date(2026,10,12))
        self.assertNotEqual(r['state']['step'], 'qa')
        self.assertIn('trigger', asked)
        self.assertNotIn('neuro_detail', asked)
        r = agent.handle_turn(None, '腰痛尿不出來', tools)
        self.assertTrue(r['done'])
        self.assertIn('急診', r['reply'])

    def test_summary_history_is_not_diagnosis(self):
        rows = {'appointments': [
            {'id':'a','patient_id':'p','appointment_no':1,'department':'脊椎外科','doctor':'高醫師','appointment_date':'2026-10-12','time_slot':'早上 09:00 - 12:00','status':'confirmed'},
            {'id':'old','patient_id':'p','department':'脊椎外科','doctor':'高醫師','appointment_date':'2026-10-01','status':'confirmed'}],
            'patients':[{'id':'p','patient_no':1,'name':'測試病患','birth_date':'1990-01-01'}],
            'triage_records':[
                {'patient_id':'p','appointment_id':'a','body_part':'腰椎','main_complaint':'腰痛','qa_answers':{**NORMAL,'urgent_screen':'（略過）'},'created_at':'2026-10-12T10:00:00'},
                {'patient_id':'p','appointment_id':'old','body_part':'腰椎','main_complaint':'先前腰痛','qa_answers':{'pain':'痠痛'},'created_at':'2026-10-01T10:00:00'}]}
        with patch.object(app, 'supabase', Database(rows)):
            result = app.admin_today_data('2026-10-12', None, 'test')
        item = result['items'][0]
        self.assertEqual(item['visit_type'], '有過去預約')
        self.assertEqual(len(item['previous_questionnaires']), 1)
        self.assertTrue(item['clinical_summary']['missing'])
        self.assertIn('不代表已完成就診', item['clinical_summary']['history_note'])

    def test_custom_questions_cannot_remove_safety_checks(self):
        rows = {'clinic_questions': [{'body_part':'腰椎','q_key':'pain','question':'自訂痛的位置','options':[], 'sort_order':0}]}
        with patch.object(app, 'supabase', Database(rows)):
            bank = app.get_clinic_questions()['腰椎']
        self.assertEqual(bank[0]['key'], 'emergency_screen')
        self.assertEqual(next(q for q in bank if q['key'] == 'pain')['question'], '自訂痛的位置')

if __name__ == '__main__':
    unittest.main(verbosity=2)
