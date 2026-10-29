"""問診追問：警訊題固定由規則問，其餘追問由 GPT 從診所題庫挑（或判斷可結束）；總題數 2～4 題。

範圍由程式限制：
  - 有示範規則的部位（clinical_demo.POLICIES）：每答一題先跑警訊規則（命中就中止並提醒就醫）；
    兩項警訊篩檢題固定先問，GPT 不能略過；之後的追問只能從「適用的、尚未問過的」題庫題目中挑。
  - 沒有示範規則的部位：GPT 只能從尚未問過的題庫題目中選。
  - 至少問滿 MIN_ASKED 題（有規則的部位需另外至少 1 題非警訊題）才可提前結束；問到 MAX_ASKED 題一律結束。
  - GPT 挑題時會參考 RAG 檢索到的知識片段，但題目文字一律用題庫原文；不能自己編題目。
  - 沒有金鑰、呼叫失敗或回傳不合格，就照題庫（規則）原本的順序問下一題。
"""
import json
import os
import re
from typing import Any, Dict, List

from clinical_demo import POLICIES, answered, applicable, next_choice

MIN_ASKED = 2          # 至少回答幾題（不含主訴）才可提前結束
MAX_ASKED = 4          # 最多問幾題（診所希望 2～4 題就判斷掛號醫師）
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.05"))
SCREENS = ("emergency_screen", "urgent_screen")

SYSTEM = """你是骨科診所的問診助理，使用繁體中文。
依病患目前的回答與「知識片段」，從「待問題目」中挑一個最能幫助判斷問題類型的題目；
若資訊已足夠做初步分流，action 回 "done"。只能用待問題目的 key，不可自己編題目。
輸出 JSON：{"action": "ask" 或 "done", "key": "待問題目的key（done 時留空）", "reason": "15字內的原因"}。"""


def _asked(answers: Dict[str, Any]) -> int:
    return sum(1 for k, v in answers.items() if k != "mainComplaint" and answered(v))


def _done(extra: Dict[str, Any] = None) -> Dict[str, Any]:
    return {"action": "done", "key": None, "reason": "", "source": "default", **(extra or {})}


def _gpt_pick(part, answers, candidates, chunks, client, asked, require_min) -> Dict[str, Any]:
    """從 candidates 中挑；失敗回傳 None（呼叫端用規則順序）。"""
    if not (client or os.getenv("OPENAI_API_KEY")):
        return None
    usable = [c for c in chunks if (c.get("score") or 0) >= MIN_SCORE and c.get("content")]
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(timeout=5, max_retries=0)
        given = "\n".join(f"- {('主訴' if k == 'mainComplaint' else k)}：{v}" for k, v in answers.items())
        pool = "\n".join(f"- {q['key']}：{q['question']}" for q in candidates)
        snippets = "\n".join(f"[{i}] {c['content']}" for i, c in enumerate(usable[:3], 1)) or "（無）"
        user = f"部位：{part}\n目前回答：\n{given}\n待問題目：\n{pool}\n知識片段：\n{snippets}"
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None
    keys = {q["key"] for q in candidates}
    if data.get("action") == "done":
        if asked >= require_min and usable:
            return {"action": "done", "key": None, "reason": str(data.get("reason", ""))[:30], "source": "gpt"}
        return None
    if data.get("action") == "ask" and data.get("key") in keys:
        return {"action": "ask", "key": data["key"], "reason": re.sub(r"\s+", "", str(data.get("reason", "")))[:30], "source": "gpt"}
    return None


def choose_next(part: str, answers: Dict[str, str], remaining: List[Dict[str, Any]],
                chunks: List[Dict[str, Any]], client: Any = None) -> Dict[str, Any]:
    asked = _asked(answers)
    if part in POLICIES:
        rule = next_choice(answers, remaining, part)   # 警訊規則 + 適用的下一題（題庫順序）
        if rule["action"] == "alert":
            return rule
        pending_screen = [q for q in remaining if q["key"] in SCREENS and not answered(answers.get(q["key"]))]
        if pending_screen:                              # 警訊篩檢題固定先問，GPT 不能略過
            return {"action": "ask", "key": pending_screen[0]["key"], "source": "clinical_rules", "reason": ""}
        if asked >= MAX_ASKED or rule["action"] == "done":
            return {**rule, "action": "done"}
        candidates = [q for q in remaining if q["key"] not in SCREENS and applicable(part, q["key"], answers)]
        pick = _gpt_pick(part, answers, candidates, chunks, client, asked, require_min=max(MIN_ASKED, len(SCREENS)) + 1)
        return pick or rule
    # 沒有示範規則的部位
    if asked >= MAX_ASKED or not remaining:
        return _done()
    pick = _gpt_pick(part, answers, remaining, chunks, client, asked, require_min=MIN_ASKED)
    return pick or {"action": "ask", "key": remaining[0]["key"], "reason": "", "source": "default"}
