"""GPT 的問診判斷，由 RAG 限制範圍（RAG 為主）。

  - 候選的問題類型只來自「檢索到的知識片段」所屬的類型：GPT 只能在這幾個裡面選，不能自己創造。
  - GPT 必須回報判斷依據是哪幾筆片段，且其中至少一筆要屬於它選的類型。
  - 理由文字再經程式檢查：不能出現藥物劑量、診斷用語、保證療效，數字必須來自片段或病患描述。
  - 任何一項不過、相似度太低、沒有金鑰或呼叫失敗，一律不採用 GPT，維持原本的規則判斷。
  - 急症不經過這裡（由問診規則先處理）。
"""
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

DISCLAIMER = "以上為一般衛教，不能取代醫師診斷。"
MAX_CHARS = 160
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.05"))

BANNED = re.compile(
    r"\d+\s*(?:mg|毫克|公克|克|ml|毫升|顆|錠|粒|膠囊|cc)|(?:服用|吃|口服|注射)[^。]{0,8}\d|"
    r"確診|你得了|您得了|診斷為|一定會好|保證|治癒率|百分之|不需要就醫|不用就醫|不必看醫生|不用看醫生")

SYSTEM = """你是骨科診所的問診判斷助理，使用繁體中文。
你只能在「候選類型」中選一個最符合病患描述的問題類型，並只能根據「知識片段」說明理由。
不可加入片段沒有的醫療資訊、藥名、劑量、診斷或療效保證。
所有候選都與病患描述不相關時，department 留空。理由 40～100 字。
輸出 JSON：{"department": "候選類型之一", "reason": "理由", "used": [用到的片段編號]}。"""


def validate(text: str, used: Any, chunks: List[Dict[str, Any]], question: str) -> Optional[str]:
    """理由文字通過回傳整理後的文字，否則回傳 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if not isinstance(used, list) or not used or not all(isinstance(i, int) and 1 <= i <= len(chunks) for i in used):
        return None
    if len(text) > MAX_CHARS * 2 or BANNED.search(text):
        return None
    source = question + " " + " ".join(c.get("content", "") for c in chunks)
    if any(n not in source for n in re.findall(r"\d+", text)):
        return None
    return text


def judge(question: str, chunks: List[Dict[str, Any]], rule_dept: Optional[str], label_fn: Callable[[str], str],
          client: Any = None) -> Optional[Dict[str, Any]]:
    """回傳 {department, label, reason, agrees_with_rule}；任何環節不合格就回傳 None（呼叫端維持規則判斷）。"""
    usable = [c for c in chunks if (c.get("score") or 0) >= MIN_SCORE and c.get("content") and c.get("department")]
    if not usable or not (client or os.getenv("OPENAI_API_KEY")):
        return None
    candidates = list(dict.fromkeys(c["department"] for c in usable))
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI()
        snippets = "\n".join(f"[{i}]（類型：{c['department']}）{c['content']}" for i, c in enumerate(usable, 1))
        user = f"病患描述：{question}\n候選類型：{'、'.join(candidates)}\n知識片段：\n{snippets}"
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        data = json.loads(resp.choices[0].message.content or "{}")
        dept, used = data.get("department"), data.get("used")
        if dept not in candidates:  # 超出 RAG 範圍
            return None
        reason = validate(data.get("reason"), used, usable, question)
        if not reason or not any(usable[i - 1]["department"] == dept for i in used):  # 依據必須屬於所選類型
            return None
    except Exception:
        return None
    return {"department": dept, "label": label_fn(dept), "reason": reason + DISCLAIMER, "agrees_with_rule": dept == rule_dept}
