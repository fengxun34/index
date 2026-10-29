"""問診追問範圍檢查測試（不需要金鑰）：python test_triage_questions.py"""
import json
import types

import triage_questions as t
from clinical_demo import POLICIES

CHUNKS = [{"content": "膝關節疼痛……", "score": 0.4}]


def client(payload):
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)]))))


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


# ---- 沒有示範規則的部位 ----
REMAIN = [{"key": "onset", "question": "怎麼開始？"}, {"key": "trigger", "question": "什麼動作加重？"}]
A3 = {"mainComplaint": "x", "pain": "刺痛", "radiate": "沒有", "duration": "一週"}
A1 = {"mainComplaint": "x", "pain": "刺痛"}
A2 = {**A1, "radiate": "沒有"}
run0 = lambda payload, answers=A3, remaining=REMAIN, chunks=CHUNKS: t.choose_next("未設定規則", answers, remaining, chunks, client=client(payload))
check("GPT 從待問題目挑題", run0({"action": "ask", "key": "trigger"})["key"] == "trigger")
check("挑題庫以外的題 → 退回原順序", run0({"action": "ask", "key": "亂編"})["key"] == "onset")
check("回答不足 2 題 → 不能提前結束", run0({"action": "done"}, answers=A1)["action"] == "ask")
check("回答滿 2 題且有片段 → 可提前結束", run0({"action": "done"}, answers=A2)["action"] == "done")
check("已問滿 4 題 → 一律結束", run0({"action": "ask", "key": "onset"}, answers={**A3, "onset": "a"})["action"] == "done")

# ---- 有示範規則的部位（膝關節）----
Q = POLICIES["膝關節"]["questions"]
base = {"mainComplaint": "膝蓋痛"}
rule = lambda payload, answers, remaining: t.choose_next("膝關節", answers, remaining, CHUNKS, client=client(payload))
remaining = lambda answers: [q for q in Q if q["key"] not in answers]
r = rule({"action": "ask", "key": "weight"}, base, remaining(base))
check("警訊篩檢題固定先問，GPT 不能略過", r["key"] == "emergency_screen" and r["source"] == "clinical_rules")
a = {**base, "emergency_screen": "以上都沒有"}
r = rule({"action": "ask", "key": "weight"}, a, remaining(a))
check("第二項警訊題也固定先問", r["key"] == "urgent_screen")
a = {**a, "urgent_screen": "以上都沒有"}
r = rule({"action": "ask", "key": "weight"}, a, remaining(a))
check("警訊題後，GPT 可從適用題中挑", r["key"] == "weight" and r["source"] == "gpt")
ai = {**a, "injury": "沒有受傷"}
r = rule({"action": "ask", "key": "injury_detail"}, ai, remaining(ai))
check("GPT 挑到不適用的追問（沒有受傷）→ 退回規則順序", r["key"] != "injury_detail" and r["source"] == "clinical_rules")
r = rule({"action": "done"}, a, remaining(a))
check("只問完警訊題 → 不能提前結束（至少再問 1 題）", r["action"] == "ask")
a2 = {**a, "duration": "一週內"}
r = rule({"action": "done"}, a2, remaining(a2))
check("警訊題＋1 題 → GPT 可判斷結束", r["action"] == "done")
a4 = {**a2, "injury": "沒有受傷"}
r = rule({"action": "ask", "key": "pain"}, a4, remaining(a4))
check("已問滿 4 題 → 一律結束", r["action"] == "done")
bad = {**base, "emergency_screen": "有上述至少一項"}
r = rule({"action": "ask", "key": "weight"}, bad, remaining(bad))
check("警訊題答有 → 中止並提醒就醫", r["action"] == "alert")
r = t.choose_next("膝關節", a, remaining(a), CHUNKS, client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: 1 / 0))))
check("模型出錯 → 照規則順序", r["action"] == "ask" and r["source"] == "clinical_rules")
print("全部通過")
