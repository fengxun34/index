"""LLM 版代理人的範圍檢查測試（不需要 OpenAI 金鑰，也不連資料庫）：python test_agent_llm.py"""
import json
import types
from datetime import date

import agent_llm
from agent import AgentTools

DOCTORS = {"高醫師": {"specialty": "脊椎", "departments": ["脊椎外科"]},
           "林醫師": {"specialty": "手足", "departments": ["手外科"]}}
BOOKED, RAG_CALLS = [], []


def schedule(days):
    return {"status": "success", "days": [{"date": "2099-01-05", "weekday": "一", "slots": [
        {"doctor": "高醫師", "session": "早上", "time_slot": "早上 09:00 - 12:00", "remaining": 4, "full": False},
        {"doctor": "林醫師", "session": "晚上", "time_slot": "晚上 18:00 - 21:00", "remaining": 0, "full": True}]}]}


def rag(question, meta):
    RAG_CALLS.append(question)
    if "膝" in question:
        return {"retrieved_chunks": [{"category_label": "膝", "content": "膝蓋痛可先休息冰敷", "source": "知識庫"}]}
    return {"retrieved_chunks": []}


TOOLS = AgentTools(
    questions=lambda p: [], next_available=lambda d, doc: {}, clinic_schedule=schedule,
    book=lambda payload: BOOKED.append(payload) or {"status": "success", "message": "ok"},
    history=lambda i, b: {"status": "success", "data": []}, cancel=lambda *a: {"status": "success", "message": "已取消"},
    reschedule=lambda *a: {"status": "success", "message": "已改期"}, rag=rag, doctors=DOCTORS,
    category_labels={}, referral_categories={})


class FakeClient:
    """依序回放預先寫好的模型回應：每個元素是 [(工具名, 參數), ...] 或 純文字。"""
    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(kw["messages"])
        step = self.script.pop(0)
        if isinstance(step, str):
            msg = types.SimpleNamespace(content=step, tool_calls=None)
        else:
            calls = [types.SimpleNamespace(id=f"c{i}", function=types.SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(step)]
            msg = types.SimpleNamespace(content=None, tool_calls=calls)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def turn(state, text, script):
    return agent_llm.handle_turn_llm(state, text, TOOLS, today=date(2099, 1, 1), client=FakeClient(script)), script


def tool_results(client):
    return [json.loads(m["content"]) for m in client.seen[-1] if m.get("role") == "tool"]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


slot = {"doctor": "高醫師", "date": "2099-01-05", "time_slot": "早上 09:00 - 12:00"}
SLOT_CALL = [("list_slots", {}), ]

# 1. 沒查過的時段不能掛；林醫師已額滿的時段不在清單裡
c = FakeClient([[("propose_booking", slot)], "x"]); st = agent_llm.handle_turn_llm(None, "我要掛號", TOOLS, today=date(2099, 1, 1), client=c)["state"]
check("未查詢的時段會被擋", tool_results(c)[0]["status"] == "error")

# 2. 一輪內 propose + confirm 不會掛號；缺個資會要求補
c = FakeClient([[("list_slots", {})], [("propose_booking", slot)], "x"])
r = agent_llm.handle_turn_llm(None, "我要掛號", TOOLS, today=date(2099, 1, 1), client=c); st = r["state"]
check("缺個資回報 need_info", tool_results(c)[-1]["status"] == "need_info" and not BOOKED)
check("已額滿時段不會出現在 list_slots", "林醫師" not in json.dumps(c.seen[1][-1]["content"], ensure_ascii=False))

# 3. 個資不送給模型
c = FakeClient(["好的"])
r = agent_llm.handle_turn_llm(st, "我叫王小明 A123456789 民國80年3月5日 0912-345-678", TOOLS, today=date(2099, 1, 1), client=c); st = r["state"]
sent = json.dumps(c.seen[0], ensure_ascii=False)
check("身分證／手機／生日已遮蔽", not any(x in sent for x in ("A123456789", "0912", "民國80")))
check("個資存在狀態裡", st["person"]["id_number"] == "A123456789" and st["person"]["birth"] == "1991/03/05")

# 4. propose 後，同一輪 confirm 不會執行
c = FakeClient([[("propose_booking", slot), ("confirm_pending", {})], "請確認"])
r = agent_llm.handle_turn_llm(st, "就高醫師那個時段", TOOLS, today=date(2099, 1, 1), client=c); st = r["state"]
check("同一輪不能 propose 又 confirm", not BOOKED and tool_results(c)[1]["status"] == "error")
check("提出後有確認卡片", r["card"]["type"] == "confirm" and st["pending"]["kind"] == "book")

# 5. 下一輪但病患沒有明確同意 → 不會掛
c = FakeClient([[("confirm_pending", {})], "再確認一次"])
r = agent_llm.handle_turn_llm(st, "我想想要不要換時間", TOOLS, today=date(2099, 1, 1), client=c); st = r["state"]
check("沒有明確說確定就不掛號", not BOOKED)

# 6. 明確說確定 → 掛號
c = FakeClient([[("confirm_pending", {})], "掛好了"])
r = agent_llm.handle_turn_llm(st, "確定", TOOLS, today=date(2099, 1, 1), client=c)
check("說確定後才掛號", len(BOOKED) == 1 and BOOKED[0]["doctor"] == "高醫師" and r["card"]["type"] == "booked" and r["done"])

# 7. 取消只能取消查到的本人掛號
st = agent_llm.new_state(); st["person"] = {"id_number": "A123456789", "birth": "1991/03/05"}
c = FakeClient([[("propose_cancel", {"appointment_no": 999})], "x"])
agent_llm.handle_turn_llm(st, "取消掛號", TOOLS, today=date(2099, 1, 1), client=c)
check("取消非本人（未查到）的掛號會被擋", tool_results(c)[0]["status"] == "error")

# 8. 知識庫範圍：查不到就不給模型內容，並叫它不要自己回答
c = FakeClient([[("search_knowledge", {"question": "糖尿病怎麼吃"})], "x"])
agent_llm.handle_turn_llm(None, "糖尿病怎麼吃", TOOLS, today=date(2099, 1, 1), client=c)
res = tool_results(c)[0]
check("知識庫查不到時 found=false", res["found"] is False)

# 9. 急症字眼不經過模型
c = FakeClient([])
r = agent_llm.handle_turn_llm(None, "我胸痛喘不過氣", TOOLS, today=date(2099, 1, 1), client=c)
check("急症直接回覆就醫、不呼叫模型", "119" in r["reply"] and not c.seen)

print("全部通過")
