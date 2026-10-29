"""骨科全題庫示範：固定必要問題、條件追問與病患自述摘要。待醫師確認。"""
import json
import re
from pathlib import Path

POLICIES = json.loads(Path(__file__).with_name('clinical_protocols.json').read_text(encoding='utf-8'))
VERSION = 'ortho-demo-2026-10-13-v2'
NEGATIVE = ['以上都沒有', '都沒有', '沒有', '無', '沒有上述症狀']
AFFIRMATIVE = ['有上述至少一項', '有', '是', '有上述症狀']
UNKNOWN = ['（略過）', '略過', '跳過', '跳過這題', '不確定', '不知道', '不清楚']
# 限制否定關鍵字的範圍：不能走路是功能喪失，不是「沒有症狀」。
NEGATION_PATTERN = r'沒有|沒(?:有|發燒|胸痛|麻木|無力|失禁|呼吸困難)|並無|否認|未出現|不會(?:麻木|無力|發燒|胸痛|失禁)|無上述|無.*(?:困難|失控|麻木|無力|胸痛|發燒)|不確定|不知道|不清楚|是否'
CLAUSE_PATTERN = r'[，,。；;、\n]|但是|但|不過'


def notice(part='腰椎'):
    title = POLICIES.get(part, {}).get('title', part)
    return f'{title}示範草稿，待診所醫師確認；問診資料不等於診斷。'

NOTICE = notice()
QUESTIONS = POLICIES['腰椎']['questions']  # 舊腰痛測試與匯入相容
EMERGENCY_MESSAGE = '您回答的警訊需要立即急診評估，請勿等待一般門診；必要時撥打 119。'
URGENT_MESSAGE = '您回答的情況需要儘速聯絡醫師，安排當日評估；症狀嚴重或快速惡化時請前往急診。'


def answered(value):
    value = str(value or '').strip()
    return bool(value) and value not in UNKNOWN


def positive(value, pattern):
    for clause in re.split(CLAUSE_PATTERN, str(value or '')):
        # 功能／感覺喪失不能被「沒有」當作陰性。
        clause = clause.replace('沒有感覺', '失去感覺')
        if re.search(NEGATION_PATTERN, clause):
            continue
        if pattern and re.search(pattern, clause):
            return True
    return False


def screen(value, part='腰椎', level=None):
    value = str(value or '').strip().rstrip('。.!！')
    if value in NEGATIVE:
        return 'negative'
    if value in AFFIRMATIVE:
        return 'positive'
    if not answered(value):
        return 'unknown'
    policy = POLICIES.get(part, POLICIES['未明'])
    pattern = policy[f'{level}_pattern'] if level else policy['emergency_pattern'] + '|' + policy['urgent_pattern']
    return 'positive' if positive(value, pattern) else 'unknown'


def assess(answers, part='腰椎'):
    answers = answers or {}
    policy = POLICIES.get(part, POLICIES['未明'])
    for level, msg in [('emergency', EMERGENCY_MESSAGE), ('urgent', URGENT_MESSAGE)]:
        hit = screen(answers.get(f'{level}_screen'), part, level) == 'positive'
        hit = hit or any(positive(v, policy[f'{level}_pattern']) for v in answers.values())
        hit = hit or any(str(answers.get(k) or '').strip() in values for k, values in policy.get(f'{level}_values', {}).items())
        if hit:
            return {'level':level, 'message':msg, 'policy_version':VERSION}
    unknown = any(screen(answers.get(f'{level}_screen'), part, level) == 'unknown' for level in ('emergency','urgent'))
    review = any(answered(answers.get(k)) and str(answers[k]).strip() not in NEGATIVE + ['沒有明顯變大'] for k in policy.get('review_keys', []))
    if unknown:
        return {'level':'unconfirmed', 'message':'警訊未確認，不能視為已排除。' + ('另有病史或其他風險資訊待醫師確認。' if review else ''), 'policy_version':VERSION}
    if review:
        return {'level':'review', 'message':'病患回報病史或其他風險資訊，請醫師確認，必要時安排進一步檢查或轉診。', 'policy_version':VERSION}
    return {'level':'routine', 'message':'已回答的兩項警訊篩檢未報告異常；仍需醫師評估。', 'policy_version':VERSION}


def applicable(part, key, answers):
    gate = POLICIES.get(part, {}).get('branches', {}).get(key)
    if not gate:
        return True
    value = str(answers.get(gate['field']) or '').strip()
    # 不明、略過不能當成沒有症狀，繼續追問。
    return not answered(value) or value not in gate.get('negative', [])


def needs_neuro(answers):
    return applicable('腰椎', 'neuro_detail', answers)


def next_choice(answers, remaining, part='腰椎'):
    decision = assess(answers, part)
    if decision['level'] in ('emergency','urgent'):
        return {'action':'alert', 'source':'clinical_rules', **decision}
    for q in remaining:
        if applicable(part, q['key'], answers):
            return {'action':'ask', 'key':q['key'], 'source':'clinical_rules', 'reason':notice(part)}
    return {'action':'done', 'source':'clinical_rules', **decision}


def summarize(part, answers):
    if part not in POLICIES:
        return None
    answers = answers or {}
    questions = [q for q in POLICIES[part]['questions'] if applicable(part, q['key'], answers)]
    missing = [q['question'] for q in questions if not answered(answers.get(q['key']))]
    for level in ('emergency','urgent'):
        key = f'{level}_screen'
        q = next(q for q in questions if q['key'] == key)
        if screen(answers.get(key), part, level) == 'unknown' and q['question'] not in missing:
            missing.append(q['question'])
    return {'notice':notice(part), 'version':VERSION, 'risk':assess(answers, part),
            'patient_report':[{'key':q['key'], 'label':q['question'], 'value':str(answers[q['key']])} for q in questions if answered(answers.get(q['key']))],
            'not_applicable':[q['question'] for q in POLICIES[part]['questions'] if not applicable(part,q['key'],answers)],
            'missing':missing, 'history_note':'過去預約不代表已完成就診；未包含醫師診斷、處方或正式病歷。'}


def public_rules():
    return {part:{k:v for k,v in p.items() if k != 'questions'} for part,p in POLICIES.items()}
