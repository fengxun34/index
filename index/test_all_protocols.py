"""全骨科示範測試：不用真實資料庫、LINE 或 LLM。"""
import csv
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for k in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'OPENAI_API_KEY'):
    os.environ.pop(k, None)
with patch('dotenv.load_dotenv', return_value=False):
    import app
import agent
from clinical_demo import POLICIES, assess, next_choice, summarize, applicable
from fastapi.testclient import TestClient

def normal_answers(part):
    values = {q['key']: (q.get('options') or ['沒有'])[0] for q in POLICIES[part]['questions']}
    values.update({'emergency_screen':'以上都沒有', 'urgent_screen':'以上都沒有', 'mainComplaint':POLICIES[part]['title']})
    for gate in POLICIES[part]['branches'].values():
        values[gate['field']] = gate['negative'][0]
    return values

EXAMPLES = {'頸椎':'我脖子痛','腰椎':'我腰痛','肩關節':'我肩膀痛','肘腕':'我手腕痛','髖關節':'我髖部痛',
            '膝關節':'我膝蓋痛','足踝':'我腳踝痛','外傷':'我跌倒受傷','兒童':'我的小孩膝蓋痛','腫瘤':'我摸到腫塊',
            '高壓氧':'我想評估高壓氧','未明':'我的關節不舒服'}

class AllProtocolTests(unittest.TestCase):
    def test_bank_and_sources_cover_every_part(self):
        bank = json.loads(Path(__file__).with_name('clinic_default_questions.json').read_text())
        kb = json.loads(Path(__file__).with_name('clinic_demo_knowledge.json').read_text())
        self.assertEqual(len(POLICIES), 12)
        self.assertEqual(len(kb), 60)
        for part, policy in POLICIES.items():
            with self.subTest(part=part):
                self.assertEqual(bank[part], policy['questions'])
                keys = [q['key'] for q in bank[part]]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertEqual(keys[:2], ['emergency_screen','urgent_screen'])
                self.assertTrue(policy['branches'])
                rows = [r for r in kb if r['body_part']==part]
                self.assertEqual(len(rows), 5)
                self.assertTrue(all(r['source_url'].startswith('https://') and r['review_status']=='draft' for r in rows))

    def test_risk_and_missing_for_every_part(self):
        for part in POLICIES:
            with self.subTest(part=part):
                normal = normal_answers(part)
                self.assertNotIn(assess(normal,part)['level'], ('emergency','urgent'))
                for level in ('emergency','urgent'):
                    ans={**normal,f'{level}_screen':'有上述至少一項'}
                    self.assertEqual(assess(ans,part)['level'],level)
                    self.assertEqual(next_choice(ans,POLICIES[part]['questions'][2:],part)['action'],'alert')
                self.assertEqual(assess({**normal,'emergency_screen':'不確定'},part)['level'],'unconfirmed')
                self.assertTrue(summarize(part,{**normal,'urgent_screen':'（略過）'})['missing'])
                negative={**normal,'pain':'沒有胸痛，也沒有發燒'}
                self.assertNotIn(assess(negative,part)['level'],('emergency','urgent'))

    def test_all_branches_skip_or_ask_with_unknown(self):
        for part,p in POLICIES.items():
            for key,gate in p['branches'].items():
                with self.subTest(part=part,key=key):
                    ans=normal_answers(part)
                    self.assertFalse(applicable(part,key,ans))
                    ans[gate['field']]='不確定'
                    self.assertTrue(applicable(part,key,ans))
                    choice=next_choice(ans,[next(q for q in p['questions'] if q['key']==key)],part)
                    self.assertEqual(choice['key'],key)

    def test_free_text_danger_and_function_loss(self):
        cases=[('膝關節','膝蓋痛不能承重','urgent'),('髖關節','髖部痛不能走路','emergency'),
               ('肩關節','肩痛伴隨胸痛','emergency'),('肘腕','手腕沒有感覺','urgent'),
               ('足踝','脚踝受傷明顯變形','emergency'),('外傷','受傷處麻木','emergency'),
               ('兒童','孩子發燒','emergency'),('頸椎','頸痛走路不穩','urgent'),
               ('高壓氧','疑似一氧化碳暴露','urgent'),('高壓氧','中毒後呼吸困難','emergency')]
        for part,text,level in cases:
            with self.subTest(part=part,text=text):
                self.assertEqual(assess({**normal_answers(part),'mainComplaint':text},part)['level'],level)
        for part in POLICIES:
            self.assertNotIn(assess({**normal_answers(part),'pain':'不確定是否有胸痛'},part)['level'],('emergency','urgent'))

    def test_api_all_parts_and_model_cannot_skip(self):
        with patch.object(app,'supabase',None), patch.object(app.agent_llm,'llm_enabled',return_value=True), patch.object(app.agent_llm,'handle_turn_llm',side_effect=AssertionError('症狀問診不能略過規則')):
            with TestClient(app.app) as client:
                data=client.get('/api/clinic/questions').json()
                self.assertEqual(set(data['clinical_rules']),set(POLICIES))
                for part,text in EXAMPLES.items():
                    with self.subTest(part=part):
                        r=client.post('/api/agent/chat',json={'message':text}).json()
                        self.assertEqual(r['state']['part'],part)
                        self.assertTrue(r['state']['clinical_demo'])
                        r=client.post('/api/triage/next-question',json={'body_part':part,'answers':{'emergency_screen':'以上都沒有'},'remaining_keys':['urgent_screen','duration']}).json()
                        self.assertEqual(r['key'],'urgent_screen')
                        payload={'name':'測試病患','id_number':'A123456789','department':'脊椎外科','doctor':'高醫師','slot':'2026-12-14 早上 09:00 - 12:00',
                                 'body_part':part,'qa_answers':{'emergency_screen':'有上述至少一項'}}
                        r=client.post('/api/booking/confirm',json=payload).json()
                        self.assertIn('急診',r['message'])

    def test_chat_all_parts_reach_end_without_four_question_cap(self):
        tools=agent.AgentTools(questions=lambda part:POLICIES[part]['questions'],next_available=lambda *a:{'status':'error','message':'測試無時段'},
              clinic_schedule=lambda *a:{},book=lambda *a:self.fail('不可寫入'),history=lambda *a:{},cancel=lambda *a:{},reschedule=lambda *a:{},
              rag=lambda *a:{'answer':'示範'},doctors={},category_labels={'脊椎外科':'脊椎'},referral_categories={})
        for part,text in EXAMPLES.items():
            with self.subTest(part=part):
                r=agent.handle_turn(None,text,tools)
                asked=[]; values=normal_answers(part)
                for _ in range(20):
                    if r['state']['step']!='qa':break
                    key=r['state'].get('clinical_question_key','emergency_screen')
                    asked.append(key)
                    r=agent.handle_turn(r['state'],values.get(key,'沒有'),tools)
                self.assertNotEqual(r['state']['step'],'qa')
                self.assertGreater(len(asked),4)
                expected=[q['key'] for q in POLICIES[part]['questions'] if applicable(part,q['key'],values)]
                self.assertEqual(asked,expected)
                summary=summarize(part,r['state']['answers'])
                self.assertEqual(summary['missing'],[])

    def test_rag_scoped_to_part_with_base_63(self):
        with Path(__file__).with_name('medical_sheet.csv').open(encoding='utf-8-sig') as source:
            rows=list(csv.DictReader(source))
        docs=[f"部位：{r['body_part']}。症狀：{r['keywords']}。建議：{r['advice']}。警告：{r['warning']}" for r in rows]
        with patch.object(app,'supabase',None), patch.object(app,'load_sql_data',return_value=([r['department'] for r in rows],docs,None,None)):
            app.rebuild_rag_index()
            self.assertEqual(len(app.contents),len(rows)+60)
            for part in POLICIES:
                with self.subTest(part=part):
                    hits=app.rag_search(part+' 疼痛 檢查',top_k=100,body_part=part)
                    draft=[h for h in hits if '示範草稿' in h['source']]
                    self.assertEqual(len(draft),5)
                    self.assertTrue(all('部位：'+part+'。' in h['content'] for h in draft))

if __name__=='__main__': unittest.main(verbosity=2)
