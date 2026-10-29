"""GPT 問診判斷的 RAG 範圍檢查測試（不需要金鑰）：python test_rag_generate.py"""
import json
import types

import rag_generate as g

CHUNKS = [
    {"department": "運動醫學科", "score": 0.4, "content": "部位：膝關節。症狀：上下樓梯疼痛。建議：減少爬樓梯，休息並冰敷 15 分鐘。"},
    {"department": "關節重建科", "score": 0.3, "content": "部位：膝關節。症狀：長期退化、僵硬。建議：評估關節狀況。"},
]
LABEL = lambda d: {"運動醫學科": "運動傷害", "關節重建科": "關節退化"}.get(d, d)


def client(payload):
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    create = lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def run(payload, chunks=CHUNKS, rule="運動醫學科"):
    return g.judge("膝蓋上下樓梯會痛", chunks, rule, LABEL, client=client(payload))


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


good = {"department": "運動醫學科", "reason": "上下樓梯疼痛，知識庫建議減少爬樓梯並冰敷 15 分鐘。", "used": [1]}
r = run(good)
check("合格的判斷會採用，且標示與規則一致", r and r["department"] == "運動醫學科" and r["agrees_with_rule"] and r["reason"].endswith(g.DISCLAIMER))
r = run({**good, "department": "關節重建科", "used": [2], "reason": "症狀為長期退化、僵硬，建議評估關節狀況。"})
check("與規則不同但在 RAG 範圍內 → 採用並標示不一致", r and r["department"] == "關節重建科" and not r["agrees_with_rule"])
check("選了檢索結果以外的類型 → 不採用", run({**good, "department": "脊椎外科"}) is None)
check("依據的片段不屬於所選類型 → 不採用", run({**good, "department": "關節重建科", "used": [1]}) is None)
check("沒有引用片段 → 不採用", run({**good, "used": []}) is None)
check("引用不存在的片段 → 不採用", run({**good, "used": [9]}) is None)
check("理由出現藥物劑量 → 不採用", run({**good, "reason": "可吃止痛藥 500 mg。"}) is None)
check("理由出現診斷用語 → 不採用", run({**good, "reason": "您確診為退化性關節炎。"}) is None)
check("理由的數字不在片段中 → 不採用", run({**good, "reason": "請冰敷 40 分鐘。"}) is None)
check("相似度太低 → 不呼叫模型", run(good, [{**CHUNKS[0], "score": 0.01}]) is None)
check("沒有片段 → 不呼叫模型", run(good, []) is None)
print("全部通過")
