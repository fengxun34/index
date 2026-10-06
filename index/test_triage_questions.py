"""問診追問的範圍檢查測試（不需要金鑰）：python test_triage_questions.py"""
import json
import types

import triage_questions as t

REMAIN = [{"key": "onset", "question": "怎麼開始？"}, {"key": "trigger", "question": "什麼動作加重？"}]
CHUNKS = [{"content": "膝關節疼痛……", "score": 0.4}]
A3 = {"mainComplaint": "膝蓋痛", "pain": "刺痛", "radiate": "沒有", "duration": "一週"}
A4 = {**A3, "onset": "運動後"}
A1 = {"mainComplaint": "膝蓋痛", "pain": "刺痛"}


def client(payload):
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)]))))


def run(payload, answers=A3, remaining=REMAIN, chunks=CHUNKS):
    return t.choose_next("膝關節", answers, remaining, chunks, client=client(payload))


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


r = run({"action": "ask", "key": "trigger", "reason": "確認誘因"})
check("GPT 從待問題目挑題", r["key"] == "trigger" and r["source"] == "gpt")
r = run({"action": "ask", "key": "自己編的題目"})
check("挑了題庫以外的題 → 退回原順序", r["key"] == "onset" and r["source"] == "default")
r = run({"action": "done"})
check("回答夠多且有知識片段 → 可提前結束", r["action"] == "done")
r = run({"action": "done"}, answers=A1)
check("回答不足 2 題 → 不能提前結束", r["action"] == "ask" and r["key"] == "onset")
r = run({"action": "done"}, answers={**A1, "radiate": "沒有"})
check("回答滿 2 題且有片段 → 可提前結束", r["action"] == "done")
r = run({"action": "ask", "key": "onset"}, answers=A4)
check("已問滿 4 題 → 一律結束", r["action"] == "done")
r = run({"action": "done"}, chunks=[])
check("沒有知識片段 → 不能提前結束", r["action"] == "ask")
check("沒有待問題目 → 結束", t.choose_next("膝關節", A3, [], CHUNKS, client=client({}))["action"] == "done")
bad = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: 1 / 0)))
check("模型出錯 → 退回原順序", t.choose_next("膝關節", A3, REMAIN, CHUNKS, client=bad)["key"] == "onset")
print("全部通過")
