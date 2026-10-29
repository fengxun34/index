"""問診自由回答對應的離線測試（假 OpenAI，不需要金鑰、不連資料庫）：python test_answer_mapper.py"""
import json
import types

import answer_mapper as m

CALLS = []


def fake(option):
    def create(**kw):
        CALLS.append(kw)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps({"option": option})))])
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


WEIGHT = ["能正常承重", "可承重但疼痛", "不能承重或走路", "不確定"]
Q = "能否站立承重與走路？"
fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


check("自由回答可對應到選項", m.map_answer("膝關節", "weight", Q, WEIGHT, "走路會痛但還是可以走", fake("可承重但疼痛")) == "可承重但疼痛")
n = len(CALLS)
check("已經是選項就不呼叫模型", m.map_answer("膝關節", "weight", Q, WEIGHT, "能正常承重", fake("不確定")) is None and len(CALLS) == n)
check("略過／不確定字眼不呼叫模型", m.map_answer("膝關節", "weight", Q, WEIGHT, "不知道", fake("不確定")) is None and len(CALLS) == n)
check("沒有選項的題目不呼叫模型", m.map_answer("膝關節", "pain", "哪一側？", [], "右邊", fake("x")) is None and len(CALLS) == n)
check("模型回選項以外的文字會被拒絕", m.map_answer("膝關節", "weight", Q, WEIGHT, "有點痛", fake("完全正常")) is None)
check("模型回 none 就維持原文", m.map_answer("膝關節", "weight", Q, WEIGHT, "有點痛", fake("none")) is None)
n = len(CALLS)
check("原話已命中急症規則就不對應（保留原文）", m.map_answer("膝關節", "weight", Q, WEIGHT, "完全不能走路，腳發紫冰冷", fake("可承重但疼痛")) is None and len(CALLS) == n)
check("警訊篩檢題不能被對應成「以上都沒有」",
      m.map_answer("膝關節", "emergency_screen", "警訊？", ["以上都沒有", "有上述至少一項", "不確定"], "我沒有這些症狀", fake("以上都沒有")) is None)
check("警訊篩檢題可以對應成「有上述至少一項」",
      m.map_answer("膝關節", "emergency_screen", "警訊？", ["以上都沒有", "有上述至少一項", "不確定"], "走路時整個膝蓋怪怪的還很燙", fake("有上述至少一項")) == "有上述至少一項")
check("「沒有」類選項需要原話有否定字眼",
      m.map_answer("膝關節", "locking", "卡住？", ["沒有卡住或不穩", "偶爾卡卡或不穩", "不確定"], "走樓梯時感覺怪怪的", fake("沒有卡住或不穩")) is None)
check("原話有否定字眼時可對應「沒有」類選項",
      m.map_answer("膝關節", "locking", "卡住？", ["沒有卡住或不穩", "偶爾卡卡或不穩", "不確定"], "膝蓋沒有卡住也不會軟腳", fake("沒有卡住或不穩")) == "沒有卡住或不穩")
check("含「請補充」的選項不對應（保留原話）",
      m.map_answer("膝關節", "swelling", "腫脹？", ["沒有腫脹或發熱", "有，請補充", "不確定"], "昨天開始腫起來", fake("有，請補充")) is None)
check("模型出錯就回 None", m.map_answer("膝關節", "weight", Q, WEIGHT, "有點痛", types.SimpleNamespace(chat=None)) is None)

a = {}
m.store(a, "weight", "走路會痛但還能走", "可承重但疼痛")
check("對應成功：存選項並另存原話", a == {"weight": "可承重但疼痛", "weight__text": "走路會痛但還能走"})
m.store(a, "weight", "再說一次", None)
check("沒對應：存原文並清掉舊的原話", a == {"weight": "再說一次"})

from clinical_demo import assess, summarize
r = assess({"weight": "可承重但疼痛", "weight__text": "晚上痛到不能走路，還發燒"}, "膝關節")
check("對應後規則仍掃描原話找警訊", r["level"] in ("urgent", "emergency"))
s = summarize("膝關節", {"weight": "可承重但疼痛", "weight__text": "走路會痛但還能走"})
check("摘要顯示病患原話", any("病患原話：走路會痛但還能走" in x["value"] for x in s["patient_report"]))
print("全部通過" if not fails else f"{fails} 項失敗")
raise SystemExit(1 if fails else 0)
