"""聊天式掛號助理（OpenAI 大型語言模型版）。

沿用 agent.py 的「工具」層（AgentTools），只把「決策」交給模型。模型只能透過下面的工具行動，
而且有幾條規則由程式強制執行（不是靠提示詞）：

  掛號範圍：
    - 只能掛本診所的醫師；時段必須是這次對話中 list_slots 查到、仍有名額的時段（資料庫仍會再檢查一次容額）。
    - 掛號／取消／改期都要兩步：先 propose_*（向病患確認），病患下一句明確說「確定」，才由 confirm_pending 執行。
    - 取消、改期的對象必須是 list_my_appointments 查到的本人掛號。
  回答範圍：
    - 醫療相關問題只能根據 search_knowledge（診所知識庫）回答；查不到就請病患洽診所或掛號由醫師評估。
    - 不做診斷、不開藥、不回答非骨科或與診所無關的問題。
    - 疑似急症的字眼，直接由程式回覆就醫提醒，不經過模型。
  個資：
    - 身分證、生日、手機只由程式擷取並存在對話狀態，送給模型的文字已遮蔽，工具也不會把這些資料回傳給模型。

沒有設定 OPENAI_API_KEY 或呼叫失敗時，由 app.py 退回 agent.py 的規則版。
"""

import json
import os
import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from agent import NO, YES, AgentTools, normalize_birth, parse_person, reply, spoken_date

MAX_HISTORY = 12
MAX_STEPS = 6
MAX_SLOTS_SHOWN = 12

EMERGENCY = re.compile(r"胸痛|胸悶|呼吸困難|喘不過氣|大量出血|骨頭外露|意識不清|昏倒|大小便失禁|無法動彈|肢體發黑|自殺|不想活")
EMERGENCY_REPLY = ("您描述的情況可能很緊急，請立即撥打 119 或前往最近的急診，不要等門診。"
                   "等狀況穩定、確定是骨科問題後，我再協助您掛號。")
CHANGE_WORDS = re.compile(r"換|改|不|等等|先")

SYSTEM_PROMPT = """你是社區型骨科診所的線上掛號助理，使用繁體中文，語氣親切簡短。

你能做的事只有：回答診所與骨科常見問題、協助掛號、查詢／取消／改期掛號。
規則：
1. 醫療或衛教問題，一定先呼叫 search_knowledge，只能用它回傳的內容回答，並說明這是一般衛教、不能取代醫師診斷。
   查不到內容（found=false）就說明無法回答，建議掛號由醫師評估。不要診斷、不要開藥或建議劑量、不要保證療效。
2. 與骨科或診所無關的問題（天氣、程式、其他科別的治療等），簡短婉拒，並把話題帶回掛號或骨科問題。
3. 問診：病患只說「哪裡痛」時不要馬上推薦時段。先用 search_knowledge 查相關知識，再一次問一個問題，
   優先釐清：怎麼開始的（外傷或慢性）、持續多久、什麼動作加重、有無腫脹／麻木／夜間痛、對日常生活的影響、相關病史。
   問 2～4 題就好（最多 4 題）、資訊足夠就推薦醫師，病患不想答就跳過。判斷只能依知識庫內容。
4. 掛號流程：了解症狀與需求 → list_slots 查時段 → 向病患提出一個具體時段 → 收集姓名、身分證、生日、手機（病患直接輸入，
   系統會自行保存，你不需要也看不到這些資料；缺什麼 propose_booking 會告訴你。已登入、資料已收集齊時，絕對不要再向病患索取，直接 propose_booking）→ propose_booking → 請病患確認。
5. 病患明確說「確定」之後，才呼叫 confirm_pending。不要在同一輪裡 propose 又 confirm。病患沒有明說確定就不要掛。
6. 只能安排 list_slots 回傳的時段與診所的醫師。不要自己編時段或醫師。醫師與專長以工具回傳為準。
7. 取消、改期要先 list_my_appointments，再 propose_cancel／propose_reschedule，同樣要病患說「確定」。
8. 不要複述病患的身分證、生日或手機。
9. 回覆控制在 120 字內；一次只問一個問題。"""

TOOL_SPECS: List[Dict[str, Any]] = [
    {"name": "search_knowledge", "description": "查詢診所知識庫，回答骨科衛教或診所相關問題。",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "list_slots", "description": "查詢未來 14 天仍有名額的門診時段，由近到遠排序。可指定醫師、日期（YYYY-MM-DD）、時段（早上/下午/晚上）。",
     "parameters": {"type": "object", "properties": {
         "doctor": {"type": "string"}, "date": {"type": "string"},
         "session": {"type": "string", "enum": ["早上", "下午", "晚上"]}}}},
    {"name": "propose_booking", "description": "提出要掛的時段，請病患確認。時段必須來自 list_slots。",
     "parameters": {"type": "object", "properties": {
         "doctor": {"type": "string"}, "date": {"type": "string"}, "time_slot": {"type": "string"},
         "name": {"type": "string", "description": "病患姓名；已知道就不用再傳"},
         "department": {"type": "string", "description": "選填，該醫師負責的科別"}},
         "required": ["doctor", "date", "time_slot"]}},
    {"name": "list_my_appointments", "description": "查詢病患即將到來的掛號（需已提供身分證與生日）。",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_cancel", "description": "提出要取消的掛號，請病患確認。",
     "parameters": {"type": "object", "properties": {"appointment_no": {"type": "integer"}}, "required": ["appointment_no"]}},
    {"name": "propose_reschedule", "description": "提出要改到的新時段（需來自 list_slots），請病患確認。",
     "parameters": {"type": "object", "properties": {
         "appointment_no": {"type": "integer"}, "doctor": {"type": "string"},
         "date": {"type": "string"}, "time_slot": {"type": "string"}},
         "required": ["appointment_no", "doctor", "date", "time_slot"]}},
    {"name": "confirm_pending", "description": "病患在上一輪確認之後，明確說「確定」時，才執行待確認的掛號／取消／改期。",
     "parameters": {"type": "object", "properties": {}}},
]


# ===== 個資遮蔽 =====

_ID = re.compile(r"[A-Za-z][12]\d{8}")
_PHONE = re.compile(r"09\d{2}[\s-]?\d{3}[\s-]?\d{3}")
_BIRTH = re.compile(r"民國\s*\d{2,3}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日號]?|\d{4}\s*[年/\-.]\s*\d{1,2}\s*[月/\-.]\s*\d{1,2}\s*[日號]?")


def mask_sensitive(text: str, found: Dict[str, str]) -> str:
    """遮蔽身分證、手機、生日；程式抓到個資但正則遮不掉（例如口語中文數字）時，整句不送給模型。"""
    masked = _BIRTH.sub("[生日]", _PHONE.sub("[手機]", _ID.sub("[身分證]", text)))
    if masked == text and any(k in found for k in ("id_number", "phone", "birth")):
        return "（病患已提供個人資料）"
    return masked


def missing_person_fields(person: Dict[str, str]) -> List[str]:
    labels = {"name": "姓名", "id_number": "身分證字號", "birth": "生日", "phone": "手機"}
    return [label for key, label in labels.items() if not person.get(key)]


# ===== 對話狀態 =====

def new_state() -> Dict[str, Any]:
    return {"mode": "llm", "turn": 0, "history": [], "person": {}, "offered": [], "appointments": [], "pending": None}


def _slot_key(doctor: str, day: str, time_slot: str) -> str:
    return f"{doctor}|{day}|{time_slot}"


# ===== 工具實作（每個都回傳可轉成 JSON 的 dict；程式檢查都在這裡）=====

class ToolRunner:
    def __init__(self, state: Dict[str, Any], tools: AgentTools, user_msg: str):
        self.state, self.tools, self.user_msg = state, tools, user_msg
        self.card: Optional[Dict[str, Any]] = None
        self.done = False

    def run(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn: Optional[Callable[..., Dict[str, Any]]] = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"status": "error", "message": "沒有這個工具"}
        try:
            return fn(**args)
        except TypeError:
            return {"status": "error", "message": "參數不正確"}

    def t_search_knowledge(self, question: str) -> Dict[str, Any]:
        res = self.tools.rag(question, {})
        chunks = res.get("retrieved_chunks") or []
        if not chunks:
            return {"found": False, "note": "知識庫沒有相關內容，請勿自行回答，建議掛號由醫師評估。"}
        return {"found": True, "snippets": [
            {"topic": c.get("category_label"), "content": c.get("content"), "source": c.get("source")} for c in chunks[:3]]}

    def t_list_slots(self, doctor: Optional[str] = None, date: Optional[str] = None, session: Optional[str] = None):
        if doctor and doctor not in self.tools.doctors:
            return {"status": "error", "message": f"本診所醫師只有：{'、'.join(self.tools.doctors)}"}
        schedule = self.tools.clinic_schedule(14)
        found = []
        for day in schedule.get("days", []):
            if date and day["date"] != date:
                continue
            for s in day["slots"]:
                if s["full"] or (doctor and s["doctor"] != doctor) or (session and s["session"] != session):
                    continue
                found.append({"doctor": s["doctor"], "date": day["date"], "weekday": day.get("weekday"),
                              "session": s["session"], "time_slot": s["time_slot"],
                              "specialty": self.tools.doctors[s["doctor"]]["specialty"]})
        found = found[:MAX_SLOTS_SHOWN]
        keys = {_slot_key(s["doctor"], s["date"], s["time_slot"]) for s in found}
        self.state["offered"] = [k for k in self.state["offered"] if k not in keys] + sorted(keys)
        self.state["offered"] = self.state["offered"][-60:]
        return {"slots": found} if found else {"slots": [], "note": "這個條件下沒有可掛的時段"}

    def _checked_slot(self, doctor: str, day: str, time_slot: str) -> Optional[Dict[str, Any]]:
        if doctor not in self.tools.doctors:
            return {"status": "error", "message": f"本診所醫師只有：{'、'.join(self.tools.doctors)}"}
        if _slot_key(doctor, day, time_slot) not in self.state["offered"]:
            return {"status": "error", "message": "這個時段不在查詢結果中，請先用 list_slots 查詢並從中挑選。"}
        return None

    def t_propose_booking(self, doctor: str, date: str, time_slot: str, name: Optional[str] = None,
                          department: Optional[str] = None):
        if (err := self._checked_slot(doctor, date, time_slot)):
            return err
        person = self.state["person"]
        if name and not person.get("name"):
            person["name"] = name.strip()[:10]
        if (missing := missing_person_fields(person)):
            return {"status": "need_info", "missing": missing,
                    "note": "請向病患索取這些資料，病患直接輸入即可；你不需要重複內容。"}
        if person.get("birth") and not normalize_birth(person["birth"]):
            person.pop("birth")
            return {"status": "need_info", "missing": ["生日"], "note": "生日格式不正確，請再問一次。"}
        depts = self.tools.doctors[doctor]["departments"]
        dept = department if department in depts else depts[0]
        self.state["pending"] = {"kind": "book", "turn": self.state["turn"], "doctor": doctor, "date": date,
                                 "time_slot": time_slot, "department": dept}
        session = time_slot.split(" ", 1)[0]
        self.card = {"type": "confirm", "doctor": doctor, "date": date, "session": session, "time_slot": time_slot,
                     "name": person["name"], "phone": person.get("phone")}
        return {"status": "waiting_confirmation", "note": "請向病患複述醫師與時段，問是否確定掛號。"}

    def _identified(self) -> Optional[Dict[str, Any]]:
        p = self.state["person"]
        if not p.get("id_number") or not p.get("birth"):
            return {"status": "need_info", "missing": ["身分證字號", "生日"],
                    "note": "請病患直接輸入身分證字號與生日（不必重複內容）。"}
        return None

    def t_list_my_appointments(self):
        if (err := self._identified()):
            return err
        p = self.state["person"]
        res = self.tools.history(p["id_number"], p["birth"])
        if res.get("status") != "success":
            p.pop("birth", None)
            return {"status": "error", "message": res.get("message", "查詢失敗")}
        upcoming = sorted((h for h in res.get("data", []) if not h.get("is_past") and h.get("status") == "confirmed"),
                          key=lambda h: h["slot"])
        self.state["appointments"] = [
            {"no": h["appointment_no"], "doctor": h["doctor"], "date": h["appointment_date"],
             "time_slot": h["time_slot"], "department": h["department"]} for h in upcoming]
        return {"appointments": [{"appointment_no": a["no"], "doctor": a["doctor"], "date": a["date"],
                                  "time_slot": a["time_slot"]} for a in self.state["appointments"]]}

    def _own_appointment(self, no: int) -> Optional[Dict[str, Any]]:
        return next((a for a in self.state["appointments"] if a["no"] == no), None)

    def t_propose_cancel(self, appointment_no: int):
        if (err := self._identified()):
            return err
        a = self._own_appointment(appointment_no)
        if not a:
            return {"status": "error", "message": "找不到這筆掛號，請先 list_my_appointments 並從中選擇。"}
        self.state["pending"] = {"kind": "cancel", "turn": self.state["turn"], "no": a["no"]}
        return {"status": "waiting_confirmation", "appointment": a, "note": "請向病患確認是否取消這筆掛號。"}

    def t_propose_reschedule(self, appointment_no: int, doctor: str, date: str, time_slot: str):
        if (err := self._identified()):
            return err
        a = self._own_appointment(appointment_no)
        if not a:
            return {"status": "error", "message": "找不到這筆掛號，請先 list_my_appointments 並從中選擇。"}
        if (err := self._checked_slot(doctor, date, time_slot)):
            return err
        if (a["doctor"], a["date"], a["time_slot"]) == (doctor, date, time_slot):
            return {"status": "error", "message": "新時段與原本相同"}
        self.state["pending"] = {"kind": "reschedule", "turn": self.state["turn"], "no": a["no"], "doctor": doctor,
                                 "date": date, "time_slot": time_slot, "old_doctor": a["doctor"]}
        return {"status": "waiting_confirmation", "note": "請向病患確認是否改到這個時段。"}

    def t_confirm_pending(self):
        pend = self.state.get("pending")
        if not pend:
            return {"status": "error", "message": "目前沒有待確認的事項"}
        # 程式強制：必須是「之後的一輪」，且病患這句話明確同意、沒有夾帶改變主意的字眼
        if pend["turn"] >= self.state["turn"]:
            return {"status": "error", "message": "必須先請病患確認，等病患下一句回覆後才能執行。"}
        if not YES.search(self.user_msg.strip()) or CHANGE_WORDS.search(self.user_msg):
            return {"status": "error", "message": "病患尚未明確說「確定」，請再向病患確認。"}
        p = self.state["person"]
        self.state["pending"] = None
        if pend["kind"] == "book":
            res = self.tools.book({
                "name": p["name"], "id_number": p["id_number"], "birth_date": p["birth"], "phone": p["phone"],
                "department": pend["department"], "doctor": pend["doctor"],
                "slot": f"{pend['date']} {pend['time_slot']}"})
            if res.get("status") == "success":
                self.card = {"type": "booked", "doctor": pend["doctor"], "date": pend["date"],
                             "session": pend["time_slot"].split(" ", 1)[0], "time_slot": pend["time_slot"], "name": p["name"]}
                self.done = True
        elif pend["kind"] == "cancel":
            res = self.tools.cancel(p["id_number"], p["birth"], pend["no"])
            self.done = res.get("status") == "success"
        else:
            res = self.tools.reschedule(p["id_number"], p["birth"], pend["no"], f"{pend['date']} {pend['time_slot']}",
                                        pend["doctor"] if pend["doctor"] != pend["old_doctor"] else None)
            self.done = res.get("status") == "success"
        return {"status": res.get("status"), "message": res.get("message")}


# ===== 對話迴圈 =====

def llm_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def make_client():
    from openai import OpenAI  # 延後匯入：沒裝套件也不影響規則版
    return OpenAI()


def _call_args(history: List[Dict[str, str]], state: Dict[str, Any], today: date, profile_name: Optional[str]) -> List[Dict[str, Any]]:
    person = state["person"]
    context = (f"今天是 {today.isoformat()}（星期{'一二三四五六日'[today.weekday()]}）。"
               f"診所醫師：{'、'.join(state.get('_doctors', []))}。"
               f"已收集：{'、'.join(k for k in ['姓名', '身分證字號', '生日', '手機'] if k not in missing_person_fields(person)) or '無'}。")
    if profile_name:
        context += f"病患已登入，姓名是{profile_name}。"
    return [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context}] + history


def handle_turn_llm(state: Optional[Dict[str, Any]], message: str, tools: AgentTools,
                    profile: Optional[Dict[str, Any]] = None, today: Optional[date] = None,
                    client: Any = None, model: Optional[str] = None) -> Dict[str, Any]:
    today = today or date.today()
    state = {**new_state(), **(state or {})}
    state["turn"] += 1
    state["_doctors"] = list(tools.doctors)
    msg = (message or "").strip()
    if profile:
        state["person"] = {**{k: v for k, v in {
            "name": profile.get("name"), "id_number": profile.get("id_number"),
            "birth": profile.get("birth_date"), "phone": profile.get("phone")}.items() if v}, **state["person"]}

    if not msg:
        name = profile.get("name") if profile else None
        greeting = f"{name}您好！" if name else "您好！"
        return _finish(state, reply(state, f"{greeting}我是骨科掛號助理，可以幫您掛號、查詢、取消、改期，也能回答骨科常見問題。請問今天哪裡不舒服？",
                                    ["我膝蓋痛", "我要掛林醫師", "查詢我的掛號"]))

    if EMERGENCY.search(msg):
        return _finish(state, reply(state, EMERGENCY_REPLY, ["重新開始"]))

    found = parse_person(msg, expecting="name" if state["person"].get("name") is None and state["history"] and
                         "姓名" in (state["history"][-1].get("content") or "") else None)
    for key in ("id_number", "phone", "birth", "name"):
        if found.get(key):
            state["person"][key] = found[key]
    state["history"].append({"role": "user", "content": mask_sensitive(msg, found)})

    client = client or make_client()
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    runner = ToolRunner(state, tools, msg)
    messages = _call_args(state["history"][-MAX_HISTORY:], state, today, (profile or {}).get("name"))
    text = ""
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0.2,
            tools=[{"type": "function", "function": spec} for spec in TOOL_SPECS])
        out = resp.choices[0].message
        if not getattr(out, "tool_calls", None):
            text = (out.content or "").strip()
            break
        messages.append({"role": "assistant", "content": out.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in out.tool_calls]})
        for call in out.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            result = runner.run(call.function.name, args if isinstance(args, dict) else {})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)})
    if not text:
        text = "抱歉，我這邊處理不太順利，請再說一次，或說「重新開始」。"

    state["history"].append({"role": "assistant", "content": text})
    state["history"] = state["history"][-MAX_HISTORY:]
    quick = ["確定"] if state.get("pending") else []
    return _finish(state, reply(state, text, quick, card=runner.card, done=runner.done))


def _finish(state: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    state.pop("_doctors", None)
    return result
