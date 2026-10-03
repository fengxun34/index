"""聊天式掛號助理（規則版代理人）。

架構分兩層，之後要換成大型語言模型（例如 Claude）時，只需要換掉「決策」那一層：
  - 工具（AgentTools）：查班表、找最快時段、掛號、查紀錄、取消、改期、RAG 建議，全部由 app.py 提供，
    直接沿用既有的後端邏輯（身分驗證、防超賣、班表檢查都在工具裡把關）。
  - 決策（handle_turn）：依目前的對話狀態與使用者這句話，決定下一步要問什麼、要呼叫哪個工具。

對話狀態（state）每一輪由前端原封不動帶回來，後端不需要另外存 session。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

# ===== 工具介面（由 app.py 注入實作）=====


@dataclass
class AgentTools:
    questions: Callable[[str], List[Dict[str, Any]]]                 # 部位 → 問診題目
    next_available: Callable[[str, Optional[str]], Dict[str, Any]]   # (問題類型, 醫師) → 最快時段
    clinic_schedule: Callable[[int], Dict[str, Any]]                 # 天數 → 全診所班表
    book: Callable[[Dict[str, Any]], Dict[str, Any]]                 # 掛號
    history: Callable[[str, str], Dict[str, Any]]                    # (身分證, 生日) → 紀錄
    cancel: Callable[[str, str, int], Dict[str, Any]]
    reschedule: Callable[[str, str, int, str, Optional[str]], Dict[str, Any]]
    rag: Callable[[str, Dict[str, Any]], Dict[str, Any]]             # (問題, metadata) → RAG 建議
    doctors: Dict[str, Dict[str, Any]]                               # 醫師設定（DOCTORS）
    category_labels: Dict[str, str]
    referral_categories: Dict[str, str]


# ===== 症狀判斷（與 index.html 的規則一致）=====

NON_ORTHO = [
    (["頭痛", "頭暈", "偏頭痛", "中風", "半邊手腳無力"], "神經內科"),
    (["胸悶", "心悸", "胸痛", "心臟不舒服"], "心臟內科"),
    (["肚子痛", "腹痛", "拉肚子", "腹瀉", "嘔吐", "噁心想吐"], "腸胃內科"),
    (["發燒", "畏寒", "感冒", "喉嚨痛", "咳嗽", "流鼻水"], "家醫科／內科"),
    (["皮膚癢", "紅疹", "起疹子", "濕疹", "蕁麻疹"], "皮膚科"),
    (["視力模糊", "眼睛痛", "看不清楚", "眼睛紅"], "眼科"),
    (["牙齒痛", "蛀牙", "牙齦腫"], "牙科"),
]

PART_RULES = [
    ("兒童", ["小孩", "兒童", "小朋友", "O型腿", "X型腿", "扁平足", "斜頸", "生長痛"]),
    ("腫瘤", ["腫塊", "腫瘤", "骨癌", "骨髓炎"]),
    ("高壓氧", ["高壓氧", "一氧化碳中毒", "放射性骨壞死"]),
    ("頸椎", ["脖子", "頸", "落枕"]),
    ("腰椎", ["腰", "下背", "坐骨神經"]),
    ("肩關節", ["肩", "五十肩"]),
    ("肘腕", ["手肘", "手腕", "手指", "媽媽手", "腕隧道", "手"]),
    ("髖關節", ["髖", "鼠蹊", "大腿根"]),
    ("膝關節", ["膝", "退化性關節炎"]),
    ("足踝", ["腳踝", "足底", "腳跟", "扭傷", "腳"]),
    ("外傷", ["骨折", "車禍", "跌倒", "外傷", "撞擊"]),
]

# 部位代碼 → 對話裡的說法
PART_DISPLAY = {"兒童": "兒童骨骼", "腫瘤": "骨骼腫塊", "高壓氧": "需要高壓氧治療", "外傷": "外傷", "未明": "骨骼關節"}

SYMPTOM_WORDS = re.compile(r"不舒服|難受|好痛|很痛|痛|刺痛|悶痛|脹痛|酸|痠|麻|無力|腫|卡住|變形|瘀青|扭到|拐到|閃到")


def detect_part(text: str) -> Optional[str]:
    for part, words in PART_RULES:
        if any(w in text for w in words):
            return part
    return None


def has_symptom(text: str) -> bool:
    return detect_part(text) is not None or bool(SYMPTOM_WORDS.search(text))


def detect_non_ortho(text: str) -> Optional[str]:
    if detect_part(text):
        return None
    for words, dept in NON_ORTHO:
        if any(w in text for w in words):
            return dept
    return None


def check_emergency(part: str, answers: Dict[str, str]) -> str:
    joined = " ".join(str(v) for v in answers.values())
    if part in ("頸椎", "腰椎") and re.search(r"大小便失禁|下肢無力癱瘓|馬鞍區麻木|意識不清", joined):
        return "您描述的症狀可能是脊髓或神經壓迫（例如大小便失禁、下肢無力），請立即前往急診評估。"
    if part == "外傷" and re.search(r"骨頭外露|傷口大量出血|肢體發黑冰冷|摸不到脈搏|明顯變形", joined):
        return "這是嚴重外傷的徵象，請立即撥打 119 或前往急診。"
    if part in ("膝關節", "髖關節", "足踝") and re.search(r"完全無法承重|明顯變形|劇烈腫脹", joined):
        return "可能有骨折或嚴重關節損傷，請盡快前往急診評估。"
    if re.search(r"高燒不退|發黑麻木", joined):
        return "症狀較危急，建議盡快就醫，必要時前往急診。"
    if part == "高壓氧" and re.search(r"意識不清|呼吸困難", joined):
        return "可能是一氧化碳中毒急症，請立即撥打 119 或前往急診。"
    return ""


def calculate(part: str, answers: Dict[str, str]) -> List[str]:
    """回傳依分數排序的問題類型（與前端 calculate() 相同的規則）。"""
    score = {"脊椎外科": 30, "運動醫學科": 30, "關節重建科": 30, "手外科": 25, "足踝外科": 25, "骨折創傷科": 25,
             "骨質疏鬆症門診": 20, "兒童骨科": 20, "骨骼腫瘤科": 20, "高壓氧治療中心": 15}
    j = " ".join(str(v) for v in answers.values())
    has = lambda pattern: re.search(pattern, j) is not None
    if part == "頸椎":
        score["脊椎外科"] += 35 + (15 if has(r"麻|無力|延伸") else 0)
    elif part == "腰椎":
        score["脊椎外科"] += 35 + (15 if has(r"麻|延伸|坐骨") else 0)
        if has(r"骨鬆|骨質疏鬆|停經"):
            score["骨質疏鬆症門診"] += 15
    elif part == "肩關節":
        score["運動醫學科"] += 30 + (15 if has(r"跌倒|運動|搬重物") else 0)
        if has(r"夜間痛|卡住"):
            score["關節重建科"] += 10
    elif part == "肘腕":
        score["手外科"] += 35 + (15 if has(r"重複性動作|媽媽手|腕隧道") else 0)
        if has(r"跌倒|扭傷"):
            score["骨折創傷科"] += 10
    elif part == "髖關節":
        score["關節重建科"] += 30 + (15 if has(r"退化|走路|樓梯") else 0)
    elif part == "膝關節":
        score["關節重建科"] += 30 + (10 if has(r"腫脹|發熱|無法伸直") else 0)
        if has(r"運動|扭傷"):
            score["運動醫學科"] += 15
    elif part == "足踝":
        score["足踝外科"] += 35
        if has(r"扭傷|運動"):
            score["運動醫學科"] += 10
    elif part == "外傷":
        score["骨折創傷科"] += 40 + (20 if has(r"變形|傷口|骨頭外露") else 0)
    elif part == "兒童":
        score["兒童骨科"] += 45
        if has(r"跌倒|運動|受傷"):
            score["骨折創傷科"] += 10
    elif part == "腫瘤":
        score["骨骼腫瘤科"] += 45 + (15 if has(r"變大|體重下降|疼痛") else 0)
    elif part == "高壓氧":
        score["高壓氧治療中心"] += 45
    else:
        score["脊椎外科"] += 10
        score["關節重建科"] += 10
        score["運動醫學科"] += 10
        if has(r"腰|背"):
            score["脊椎外科"] += 15
        if has(r"膝|髖"):
            score["關節重建科"] += 15
        if has(r"骨鬆|骨質疏鬆"):
            score["骨質疏鬆症門診"] += 20
    return [d for d, _ in sorted(score.items(), key=lambda x: -x[1])]


# ===== 日期、時段、個人資料的解析 =====

CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
WEEKDAY_CHAR = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}  # Python weekday()
WEEKDAY_LABEL = "一二三四五六日"


def cn_to_int(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    total, current = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def convert_cn_numbers(text: str) -> str:
    def repl(m):
        n = cn_to_int(m.group(0))
        return str(n) if n is not None else m.group(0)
    return re.sub(r"[〇零一二兩三四五六七八九十百]+(?=\s*(?:年|月|日|號))", repl, text or "")


def spoken_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
        return f"{d.month}月{d.day}日（星期{WEEKDAY_LABEL[d.weekday()]}）"
    except Exception:
        return iso


def parse_schedule(text: str, today: date, doctors: List[str]) -> Dict[str, Any]:
    t = convert_cn_numbers(re.sub(r"\s", "", text or "")).replace("醫生", "醫師")
    cmd: Dict[str, Any] = {"doctor": None, "date": None, "session": None, "earliest": False}
    for doc in doctors:
        if doc in t:
            cmd["doctor"] = doc
    if re.search(r"早上|上午|早診", t):
        cmd["session"] = "早上"
    elif re.search(r"下午|午診", t):
        cmd["session"] = "下午"
    elif re.search(r"晚上|夜診|晚診", t):
        cmd["session"] = "晚上"
    if re.search(r"最快|最近|盡快|越快越好|最早", t):
        cmd["earliest"] = True
    m = None
    if "今天" in t:
        cmd["date"] = today
    elif "明天" in t:
        cmd["date"] = today + timedelta(days=1)
    elif "後天" in t:
        cmd["date"] = today + timedelta(days=2)
    elif (m := re.search(r"(下下|下|這|本)?(?:個)?(?:週|周|星期|禮拜)([一二三四五六日天])", t)):
        target = WEEKDAY_CHAR[m.group(2)]
        monday = today - timedelta(days=today.weekday())
        if m.group(1) in ("下", "下下"):
            cmd["date"] = monday + timedelta(days=7 * (1 if m.group(1) == "下" else 2) + target)
        elif m.group(1):
            cmd["date"] = monday + timedelta(days=target)
        else:
            cmd["date"] = today + timedelta(days=(target - today.weekday()) % 7)
    elif (m := re.search(r"(\d{1,2})月(\d{1,2})(?:日|號)?", t)):
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            cmd["date"] = d if d >= today else date(today.year + 1, d.month, d.day)
        except ValueError:
            pass
    elif (m := re.search(r"(\d{1,2})號", t)):
        try:
            d = date(today.year, today.month, int(m.group(1)))
            if d < today:
                d = date(today.year + (today.month == 12), today.month % 12 + 1, int(m.group(1)))
            cmd["date"] = d
        except ValueError:
            pass
    if cmd["date"]:
        cmd["date"] = cmd["date"].isoformat()
    return cmd


def has_schedule_words(cmd: Dict[str, Any]) -> bool:
    return bool(cmd["doctor"] or cmd["date"] or cmd["session"] or cmd["earliest"])


def pick_slot(schedule: Dict[str, Any], cmd: Dict[str, Any], prefer: List[str]) -> Optional[Dict[str, Any]]:
    candidates = []
    for day in schedule.get("days", []):
        if cmd.get("date") and day["date"] != cmd["date"]:
            continue
        for slot in day["slots"]:
            if slot["full"] or (cmd.get("doctor") and slot["doctor"] != cmd["doctor"]) or \
                    (cmd.get("session") and slot["session"] != cmd["session"]):
                continue
            candidates.append({**slot, "date": day["date"]})
    if not candidates:
        return None
    rank = lambda c: 0 if cmd.get("doctor") else (prefer.index(c["doctor"]) if c["doctor"] in prefer else 99)
    return sorted(enumerate(candidates), key=lambda ic: (rank(ic[1]), ic[0]))[0][1]


def parse_person(text: str, expecting: Optional[str] = None) -> Dict[str, str]:
    t = convert_cn_numbers(text or "")
    compact = re.sub(r"[\s，、；,.\-/]", "", t)
    out: Dict[str, str] = {}
    if (m := re.search(r"([A-Za-z][12]\d{8})", compact)):
        out["id_number"] = m.group(1).upper()
    if (m := re.search(r"09\d{8}", compact)):
        out["phone"] = m.group(0)
    if (m := re.search(r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", t)):
        out["birth"] = f"{int(m.group(1)) + 1911}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    elif (m := re.search(r"(\d{4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})", t)):
        out["birth"] = f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    if (m := re.search(r"(?:我叫|我是|姓名(?:是)?|名字(?:是)?)[:：\s]*([一-鿿]{2,5})", t)):
        out["name"] = m.group(1)
    elif expecting == "name":
        bare = re.sub(r"[^一-鿿]", "", t)
        if 2 <= len(bare) <= 5 and not re.search(r"不要|取消|重新|確定|好的|可以", bare):
            out["name"] = bare
    return out


def normalize_birth(birth: str) -> Optional[str]:
    try:
        return datetime.strptime(birth.replace("-", "/"), "%Y/%m/%d").strftime("%Y/%m/%d")
    except Exception:
        return None


# ===== 決策：每一輪對話 =====

YES = re.compile(r"^(好|好的|好啊|可以|確定|確認|沒問題|對|是|是的|OK|ok|嗯|就這樣|麻煩了|謝謝|要)")
NO = re.compile(r"不要|不用|取消|算了|不對|不是|先不")
RESET = re.compile(r"重新開始|重來|從頭|回到開頭")
FIELD_LABELS = {"name": "姓名", "id_number": "身分證字號", "birth": "生日", "phone": "手機"}


def new_state() -> Dict[str, Any]:
    return {"step": "start", "intent": None, "part": None, "answers": {}, "q_index": 0, "departments": [],
            "doctor": None, "slot": None, "person": {}, "purpose": None, "appointments": [], "target": None,
            "preferred_doctor": None, "rag": ""}


def reply(state, text, quick=None, card=None, done=False):
    return {"reply": text, "state": state, "quick_replies": quick or [], "card": card, "done": done}


def handle_turn(state: Optional[Dict[str, Any]], message: str, tools: AgentTools,
                profile: Optional[Dict[str, Any]] = None, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    state = {**new_state(), **(state or {})}
    msg = (message or "").strip()
    if profile:
        state["person"] = {**{k: v for k, v in {
            "name": profile.get("name"), "id_number": profile.get("id_number"),
            "birth": profile.get("birth_date"), "phone": profile.get("phone")}.items() if v}, **state["person"]}

    if not msg or RESET.search(msg):
        greeting = f"{profile['name']}您好！" if profile and profile.get("name") else "您好！"
        return reply(new_state(), f"{greeting}我是骨科掛號助理。請直接告訴我哪裡不舒服，或想掛哪位醫師；"
                                  "也可以說「查詢掛號」、「取消掛號」或「改時間」。",
                     ["我膝蓋痛", "我要掛林醫師", "查詢我的掛號", "取消掛號", "改掛號時間"])

    step = state["step"]
    handler = {
        "start": _start, "qa": _qa, "non_ortho": _non_ortho, "referral": _referral, "propose": _propose,
        "info": _info, "confirm": _confirm, "identify": _identify, "pick": _pick, "cancel_confirm": _cancel_confirm,
        "resched_time": _resched_time, "resched_confirm": _resched_confirm, "done": _start,
    }.get(step, _start)
    try:
        return handler(state, msg, tools, today)
    except Exception as e:  # 工具出錯（例如資料庫連不到）時，不讓對話整個壞掉
        return reply(state, f"抱歉，系統暫時無法處理（{e}）。請稍後再試，或改用主畫面的按鈕操作。", ["重新開始"])


def _doctor_names(tools):
    return list(tools.doctors.keys())


def _start(state, msg, tools, today):
    state.update({k: v for k, v in new_state().items() if k != "person"})
    if re.search(r"取消", msg) and re.search(r"掛號|預約|門診", msg):
        return _begin_identify(state, "cancel", tools)
    if re.search(r"改|換", msg) and re.search(r"時間|日期|掛號|預約|改期", msg):
        return _begin_identify(state, "reschedule", tools)
    if re.search(r"查詢|查一下|我的掛號|掛號紀錄|看診紀錄|掛了什麼|什麼時候看診", msg):
        return _begin_identify(state, "check", tools)

    doctor = next((d for d in _doctor_names(tools) if d in msg.replace("醫生", "醫師")), None)
    if doctor:
        state["preferred_doctor"] = doctor
        state["departments"] = [tools.doctors[doctor]["departments"][0]]
        return _offer_slot(state, tools, intro=f"好的，幫您找 {doctor} 的門診。")

    non_ortho = detect_non_ortho(msg)
    if non_ortho:
        state["step"] = "non_ortho"
        state["answers"] = {"mainComplaint": msg}
        return reply(state, f"您描述的症狀聽起來可能不是骨科問題，建議優先考慮掛「{non_ortho}」。"
                            "如果同時有骨頭或關節的問題，也可以繼續在這裡問診。",
                     ["我還是要骨科問診", "重新開始"])

    if has_symptom(msg):
        return _begin_qa(state, msg, tools)

    if re.search(r"掛號|看診|預約|看醫生|看醫師", msg):
        return reply(state, "好的！請告訴我哪裡不舒服（例如：膝蓋痛、腰痠、手腕麻），或想掛哪位醫師。",
                     ["我膝蓋痛", "我腰痛", "我要掛高醫師"])
    return reply(state, "抱歉，我還沒聽懂。您可以直接說哪裡不舒服，例如「我肩膀痛」，或說「查詢掛號」、「取消掛號」。",
                 ["我肩膀痛", "查詢我的掛號", "重新開始"])


def _begin_qa(state, msg, tools):
    part = detect_part(msg) or "未明"
    state.update({"step": "qa", "part": part, "answers": {"mainComplaint": msg}, "q_index": 0})
    questions = tools.questions(part)
    if not questions:
        return _finish_qa(state, tools)
    q = questions[0]
    return reply(state, f"了解，是{PART_DISPLAY.get(part, part)}方面的問題。我先問您幾個問題（共 {len(questions)} 題）。\n{q['question']}",
                 (q.get("options") or [])[:6] + ["跳過這題"])


def _non_ortho(state, msg, tools, today):
    if re.search(r"骨科|繼續|還是要", msg):
        return _begin_qa(state, state["answers"].get("mainComplaint", msg), tools)
    return _start(state, msg, tools, today)


def _qa(state, msg, tools, today):
    questions = tools.questions(state["part"])
    idx = state["q_index"]
    if idx < len(questions):
        state["answers"][questions[idx]["key"]] = "（略過）" if "跳過" in msg else msg
    emergency = check_emergency(state["part"], state["answers"])
    if emergency:
        state["step"] = "done"
        return reply(state, f"⚠ {emergency}\n系統不建議用一般門診掛號處理，請以就醫為優先。", ["重新開始"], done=True)
    idx += 1
    state["q_index"] = idx
    if idx < len(questions):
        q = questions[idx]
        return reply(state, f"（第 {idx + 1}/{len(questions)} 題）{q['question']}", (q.get("options") or [])[:6] + ["跳過這題"])
    return _finish_qa(state, tools)


def _finish_qa(state, tools):
    depts = calculate(state["part"], state["answers"])
    state["departments"] = depts[:3]
    top = depts[0]
    question = "。".join([f"部位：{state['part']}"] + [v for v in state["answers"].values() if v and v != "（略過）"])
    rag = tools.rag(question, {"recommended_department": top, "body_part": state["part"]})
    state["rag"] = (rag.get("answer") or "").split("\n")[0]
    label = tools.category_labels.get(top, top)
    if top in tools.referral_categories:
        state["step"] = "referral"
        return reply(state, f"判斷結果：較可能是「{label}」。{tools.referral_categories[top]}\n"
                            "您也可以先讓診所醫師初步評估，要幫您安排嗎？",
                     ["好，先讓診所醫師評估", "不用，我會去大醫院"])
    intro = f"判斷結果：較可能是「{label}」的問題。"
    if state["rag"]:
        intro += f"\n📚 {state['rag']}"
    return _offer_slot(state, tools, intro=intro)


def _referral(state, msg, tools, today):
    if NO.search(msg) and not re.search(r"評估", msg):
        state["step"] = "done"
        return reply(state, "好的，請記得攜帶先前的檢查資料到醫院就診，祝您早日康復。", ["重新開始"], done=True)
    return _offer_slot(state, tools, intro="好的。")


def _handlers_for(tools, department):
    return [d for d, info in tools.doctors.items() if department in info["departments"]]


def _offer_slot(state, tools, intro="", cmd=None, today=None):
    dept = state["departments"][0] if state["departments"] else next(iter(tools.category_labels))
    if cmd and has_schedule_words(cmd):
        prefer = [d for d in [state.get("preferred_doctor") or (state["slot"] or {}).get("doctor")] if d] + _handlers_for(tools, dept)
        found = pick_slot(tools.clinic_schedule(14), cmd, prefer)
        if not found:
            wanted = " ".join(filter(None, [spoken_date(cmd["date"]) if cmd.get("date") else "", cmd.get("session"), cmd.get("doctor")]))
            return reply(state, f"{wanted or '這個時間'}沒有可以掛的門診，請換一個時間，或說「最快的」。",
                         ["最快的", "下週一早上", "換醫師"])
        slot = {"doctor": found["doctor"], "date": found["date"], "session": found["session"], "time_slot": found["time_slot"]}
    else:
        res = tools.next_available(dept, state.get("preferred_doctor"))
        if res.get("status") != "success":
            return reply(state, f"{intro}\n抱歉，{res.get('message', '目前查不到可以掛號的時段')}", ["重新開始"])
        slot = {"doctor": res["doctor"], "date": res["date"], "session": res["session"], "time_slot": res["time_slot"]}
    state["slot"] = slot
    state["step"] = "propose"
    specialty = tools.doctors.get(slot["doctor"], {}).get("specialty", "")
    when = f"可以安排在 {spoken_date(slot['date'])}{slot['session']}" if cmd and has_schedule_words(cmd) and not cmd.get("earliest") \
        else f"最快是 {spoken_date(slot['date'])}{slot['session']}"
    text = (f"{intro}\n推薦 {slot['doctor']}（擅長：{specialty}），{when}。"
            "這個時間可以嗎？也可以直接說想要的時間，例如「下週二下午」或「換林醫師」。").strip()
    return reply(state, text, ["好，就這個時間", "下週的時間", "換其他醫師"], card={"type": "slot", **slot, "specialty": specialty})


def _propose(state, msg, tools, today):
    cmd = parse_schedule(msg, today, _doctor_names(tools))
    if re.search(r"換.*醫師|其他醫師|別的醫師", msg) and not cmd["doctor"]:
        others = [d for d in _doctor_names(tools) if d != state["slot"]["doctor"]]
        options = "、".join(f"{d}（{tools.doctors[d].get('specialty', '')}）" for d in others)
        return reply(state, f"想改掛哪一位醫師？{options}", [f"換{d}" for d in others])
    if "下週" in msg and not cmd["date"]:
        cmd["date"] = None
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        schedule = tools.clinic_schedule(14)
        days = [d for d in schedule.get("days", []) if d["date"] >= monday.isoformat()]
        prefer = [state["slot"]["doctor"]] + _handlers_for(tools, state["departments"][0])
        found = pick_slot({"days": days}, cmd, prefer)
        if found:
            cmd["date"] = found["date"]
            cmd["session"] = found["session"]
            cmd["doctor"] = found["doctor"]
    if has_schedule_words(cmd):
        if cmd["doctor"]:
            state["preferred_doctor"] = cmd["doctor"]
        return _offer_slot(state, tools, intro="好的，幫您換。", cmd=cmd)
    if YES.search(msg):
        return _ask_info(state)
    if NO.search(msg):
        return reply(state, "沒問題，請告訴我您方便的時間，例如「星期五晚上」、「10月8號早上」或「最快的」。", ["最快的", "下週一早上", "星期六早上"])
    return reply(state, "這個時間可以嗎？可以的話請說「好」，或直接說想要的時間。", ["好，就這個時間", "最快的", "換其他醫師"])


def _missing(person):
    return [f for f in ("name", "id_number", "birth", "phone") if not person.get(f)]


def _ask_info(state):
    missing = _missing(state["person"])
    if not missing:
        return _ask_confirm(state)
    state["step"] = "info"
    if len(missing) == 4:
        return reply(state, "請告訴我您的姓名、身分證字號、生日和手機，可以一次說完。\n例如：我叫王小明，A123456789，民國79年1月1日，0912345678")
    return reply(state, f"還需要您的{'、'.join(FIELD_LABELS[f] for f in missing)}。")


def _info(state, msg, tools, today):
    missing = _missing(state["person"])
    parsed = parse_person(msg, expecting="name" if missing == ["name"] or (missing and missing[0] == "name" and len(msg) <= 6) else None)
    if "birth" in parsed and not normalize_birth(parsed["birth"]):
        parsed.pop("birth")
    state["person"].update(parsed)
    if not parsed:
        return reply(state, f"抱歉沒有聽懂，請再說一次您的{'、'.join(FIELD_LABELS[f] for f in missing)}。")
    return _ask_info(state)


def _ask_confirm(state):
    state["step"] = "confirm"
    s, p = state["slot"], state["person"]
    return reply(state, f"請確認：{p['name']}，{spoken_date(s['date'])}{s['session']}，{s['doctor']}的門診。確定要掛號嗎？",
                 ["確定掛號", "換時間", "取消"],
                 card={"type": "confirm", **s, "name": p["name"], "phone": p.get("phone")})


def _confirm(state, msg, tools, today):
    cmd = parse_schedule(msg, today, _doctor_names(tools))
    if has_schedule_words(cmd) or re.search(r"換時間|改時間", msg):
        if has_schedule_words(cmd):
            return _offer_slot(state, tools, intro="好的，幫您換。", cmd=cmd)
        state["step"] = "propose"
        return reply(state, "請說您想要的時間，例如「星期五晚上」或「最快的」。", ["最快的", "下週一早上"])
    if NO.search(msg) and not re.search(r"確定", msg):
        state["step"] = "done"
        return reply(state, "好的，這次先不掛號。有需要隨時再跟我說。", ["重新開始"], done=True)
    if not YES.search(msg) and "掛號" not in msg:
        return reply(state, "要掛號請說「確定」，不要的話請說「取消」。", ["確定掛號", "取消"])
    s, p = state["slot"], state["person"]
    answers = state["answers"] if state.get("part") else {}
    res = tools.book({
        "name": p["name"], "id_number": p["id_number"], "birth_date": p["birth"], "phone": p["phone"],
        "department": (state["departments"] or [tools.doctors[s["doctor"]]["departments"][0]])[0],
        "doctor": s["doctor"], "slot": f"{s['date']} {s['time_slot']}",
        "body_part": state.get("part"), "main_complaint": answers.get("mainComplaint"),
        "qa_answers": answers or None, "ai_suggestion": state.get("rag") or None,
    })
    if res.get("status") != "success":
        state["step"] = "propose"
        return reply(state, f"掛號沒有成功：{res.get('message')}\n要換一個時間再試嗎？", ["最快的", "下週一早上", "重新開始"])
    state["step"] = "done"
    return reply(state, f"🎉 掛號完成！{spoken_date(s['date'])}{s['session']}（{s['time_slot'].split(' ', 1)[1]}），{s['doctor']}。"
                        "請提早 10 分鐘報到，祝您早日康復！",
                 ["查詢我的掛號", "重新開始"], card={"type": "booked", **s, "name": p["name"]}, done=True)


# ----- 查詢／取消／改期 -----

def _begin_identify(state, purpose, tools):
    state["purpose"] = purpose
    p = state["person"]
    if p.get("id_number") and p.get("birth"):  # 已登入（或這次對話已經給過身分）就直接查
        return _load_appointments(state, tools)
    state["step"] = "identify"
    return reply(state, "請告訴我您的身分證字號和生日，例如：A123456789，民國79年1月1日。")


def _identify(state, msg, tools, today):
    parsed = parse_person(msg)
    state["person"].update({k: v for k, v in parsed.items() if k in ("id_number", "birth")})
    p = state["person"]
    if not p.get("id_number") or not p.get("birth"):
        need = [FIELD_LABELS[f] for f in ("id_number", "birth") if not p.get(f)]
        return reply(state, f"還需要您的{'、'.join(need)}。")
    return _load_appointments(state, tools)


def _load_appointments(state, tools):
    p = state["person"]
    res = tools.history(p["id_number"], p["birth"])
    if res.get("status") != "success":
        state["person"].pop("birth", None)
        state["step"] = "identify"
        return reply(state, f"{res.get('message')} 請再說一次身分證字號和生日。")
    upcoming = [h for h in res.get("data", []) if not h.get("is_past") and h.get("status") == "confirmed"]
    upcoming.sort(key=lambda h: h["slot"])
    state["appointments"] = [{"no": h["appointment_no"], "doctor": h["doctor"], "date": h["appointment_date"],
                              "time_slot": h["time_slot"], "department": h["department"]} for h in upcoming]
    lines = [f"{i + 1}. {spoken_date(a['date'])}{a['time_slot'].split(' ')[0]}　{a['doctor']}" for i, a in enumerate(state["appointments"])]
    purpose = state["purpose"]
    if not upcoming:
        state["step"] = "done"
        last = res.get("last_visit")
        extra = f"上次看診是{spoken_date(last['appointment_date'])}，{last['doctor']}。" if last else ""
        return reply(state, f"目前沒有即將到來的掛號。{extra}要幫您掛號嗎？", ["我要掛號", "重新開始"], done=True)
    if purpose == "check":
        state["step"] = "done"
        return reply(state, "您即將到來的掛號：\n" + "\n".join(lines), ["取消掛號", "改掛號時間", "重新開始"], done=True)
    state["step"] = "pick"
    if len(upcoming) == 1:
        return _pick(state, "1", tools, None)
    action = "取消" if purpose == "cancel" else "改時間"
    return reply(state, f"您有 {len(upcoming)} 筆掛號，要{action}哪一筆？\n" + "\n".join(lines),
                 [str(i + 1) for i in range(len(upcoming))])


def _pick(state, msg, tools, today):
    m = re.search(r"\d+", msg) or re.search(r"[一二兩三四五六七八九十]+", msg)
    number = (int(m.group(0)) if m.group(0).isdigit() else cn_to_int(m.group(0))) if m else None
    idx = number - 1 if number else -1
    if not 0 <= idx < len(state["appointments"]):
        return reply(state, "請說第幾筆，例如「1」。", [str(i + 1) for i in range(len(state["appointments"]))])
    state["target"] = state["appointments"][idx]
    a = state["target"]
    when = f"{spoken_date(a['date'])}{a['time_slot'].split(' ')[0]}，{a['doctor']}"
    if state["purpose"] == "cancel":
        state["step"] = "cancel_confirm"
        return reply(state, f"確定要取消 {when} 的掛號嗎？", ["確定取消", "不要取消"])
    state["step"] = "resched_time"
    return reply(state, f"要把 {when} 改到什麼時候？例如「下週三下午」、「最快的」或「換林醫師」。", ["最快的", "下週一早上", "星期六早上"])


def _cancel_confirm(state, msg, tools, today):
    if re.search(r"不要|不用|算了", msg) and "確定" not in msg:
        state["step"] = "done"
        return reply(state, "好的，保留這筆掛號。", ["重新開始"], done=True)
    if not YES.search(msg) and "取消" not in msg:
        return reply(state, "要取消請說「確定取消」。", ["確定取消", "不要取消"])
    p, a = state["person"], state["target"]
    res = tools.cancel(p["id_number"], p["birth"], a["no"])
    state["step"] = "done"
    return reply(state, res.get("message") or "已取消。", ["我要掛號", "重新開始"], done=True)


def _resched_time(state, msg, tools, today):
    a = state["target"]
    cmd = parse_schedule(msg, today, _doctor_names(tools))
    if not has_schedule_words(cmd):
        return reply(state, "請說想改到的時間，例如「星期五晚上」或「最快的」。", ["最快的", "下週一早上"])
    prefer = [a["doctor"]] + _handlers_for(tools, a["department"])
    schedule = tools.clinic_schedule(14)
    # 改期不能選到原本那個時段（同醫師、同日期、同時段）
    for day in schedule.get("days", []):
        day["slots"] = [sl for sl in day["slots"] if not (day["date"] == a["date"] and sl["doctor"] == a["doctor"] and sl["time_slot"] == a["time_slot"])]
    found = pick_slot(schedule, cmd, prefer)
    if not found:
        return reply(state, "那個時間沒有可以掛的門診，請換一個時間，或說「最快的」。", ["最快的", "下週一早上"])
    state["slot"] = {"doctor": found["doctor"], "date": found["date"], "session": found["session"], "time_slot": found["time_slot"]}
    state["step"] = "resched_confirm"
    s = state["slot"]
    return reply(state, f"可以改到 {spoken_date(s['date'])}{s['session']}，{s['doctor']}。確定要改嗎？", ["確定", "換其他時間"],
                 card={"type": "slot", **s})


def _resched_confirm(state, msg, tools, today):
    cmd = parse_schedule(msg, today, _doctor_names(tools))
    if has_schedule_words(cmd) or re.search(r"換|其他時間", msg):
        state["step"] = "resched_time"
        return _resched_time(state, msg, tools, today) if has_schedule_words(cmd) else \
            reply(state, "請說想改到的時間。", ["最快的", "下週一早上"])
    if not YES.search(msg):
        return reply(state, "要改請說「確定」。", ["確定", "換其他時間"])
    p, a, s = state["person"], state["target"], state["slot"]
    res = tools.reschedule(p["id_number"], p["birth"], a["no"], f"{s['date']} {s['time_slot']}",
                           s["doctor"] if s["doctor"] != a["doctor"] else None)
    state["step"] = "done"
    if res.get("status") != "success":
        return reply(state, f"改期沒有成功：{res.get('message')}", ["改掛號時間", "重新開始"], done=True)
    return reply(state, f"✅ {res.get('message')}", ["查詢我的掛號", "重新開始"], done=True)
