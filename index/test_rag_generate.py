"""RAG 生成範圍檢查測試（不需要金鑰）：python test_rag_generate.py"""
import json
import types

import rag_generate as g

CHUNKS = [{"content": "部位：膝關節。症狀：上下樓梯疼痛。建議：減少爬樓梯，休息並冰敷 15 分鐘。", "score": 0.4}]


def client(payload):
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    create = lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def run(payload, chunks=CHUNKS):
    return g.explain("膝蓋上下樓梯會痛", chunks, "膝關節問題", client=client(payload))


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


ok = run({"text": "上下樓梯疼痛時可減少爬樓梯，休息並冰敷 15 分鐘。", "used": [1]})
check("合格的說明會採用並加上免責聲明", ok and ok.endswith(g.DISCLAIMER))
check("沒有引用片段 → 不採用", run({"text": "多休息。", "used": []}) is None)
check("引用不存在的片段 → 不採用", run({"text": "多休息。", "used": [5]}) is None)
check("出現藥物劑量 → 不採用", run({"text": "可吃止痛藥 500 mg。", "used": [1]}) is None)
check("出現診斷用語 → 不採用", run({"text": "您確診為退化性關節炎。", "used": [1]}) is None)
check("數字不在片段中 → 不採用", run({"text": "請冰敷 40 分鐘。", "used": [1]}) is None)
check("相似度太低 → 不呼叫模型", run({"text": "x", "used": [1]}, [{"content": "abc", "score": 0.01}]) is None)
check("沒有片段 → 不呼叫模型", run({"text": "x", "used": [1]}, []) is None)
print("全部通過")
