"""RAG 的「生成」這一半：把檢索到的知識片段，交給 GPT 整理成白話說明。

回答範圍由程式限制，不是只靠提示詞：
  - 只把檢索到的片段交給模型，要求只能根據片段，且要回報用了哪幾筆（used）。
  - 模型回傳後，程式再檢查：必須有引用片段、不能出現藥物劑量、診斷用語、保證療效，
    說明裡的數字必須來自片段或病患描述。任何一項不過，就丟掉模型的說明，改用原本的規則文字。
  - 沒有檢索到片段、相似度太低、沒有金鑰或呼叫失敗，一律不呼叫或不採用模型。
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

DISCLAIMER = "以上為一般衛教，不能取代醫師診斷。"
MAX_CHARS = 160
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.05"))

BANNED = re.compile(
    r"\d+\s*(?:mg|毫克|公克|克|ml|毫升|顆|錠|粒|膠囊|cc)|(?:服用|吃|口服|注射)[^。]{0,8}\d|"
    r"確診|你得了|您得了|診斷為|一定會好|保證|治癒率|百分之|不需要就醫|不用就醫|不必看醫生|不用看醫生")

SYSTEM = """你是骨科診所的衛教助理，使用繁體中文。
只能根據「知識片段」整理說明，不可加入片段沒有的醫療資訊、藥名、劑量、診斷或療效保證。
片段與病患描述不相關時，text 留空。語氣親切，80～140 字。
輸出 JSON：{"text": "說明", "used": [用到的片段編號]}。"""


def _clean_numbers(text: str) -> List[str]:
    return re.findall(r"\d+", text)


def validate(text: str, used: Any, chunks: List[Dict[str, Any]], question: str) -> Optional[str]:
    """通過回傳整理後的說明，否則回傳 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if not isinstance(used, list) or not used or not all(isinstance(i, int) and 1 <= i <= len(chunks) for i in used):
        return None
    if len(text) > MAX_CHARS * 2 or BANNED.search(text):
        return None
    source = question + " " + " ".join(c.get("content", "") for c in chunks)
    if any(n not in source for n in _clean_numbers(text)):
        return None
    return text[:MAX_CHARS * 2]


def explain(question: str, chunks: List[Dict[str, Any]], recommended_label: Optional[str], client: Any = None) -> Optional[str]:
    """回傳受限制的白話說明；任何環節不合格就回傳 None（呼叫端改用規則文字）。"""
    usable = [c for c in chunks if (c.get("score") or 0) >= MIN_SCORE and c.get("content")]
    if not usable or not (client or os.getenv("OPENAI_API_KEY")):
        return None
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI()
        snippets = "\n".join(f"[{i}] {c['content']}" for i, c in enumerate(usable, 1))
        user = (f"病患描述：{question}\n分流結果：{recommended_label or '尚無'}\n知識片段：\n{snippets}")
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        data = json.loads(resp.choices[0].message.content or "{}")
        text = validate(data.get("text"), data.get("used"), usable, question)
    except Exception:
        return None
    return f"{text}{DISCLAIMER}" if text else None
