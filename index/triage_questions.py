"""問診追問：由 GPT 依目前的回答，從「診所題庫」裡挑下一題（或判斷資訊已足夠）。

範圍由程式限制：
  - GPT 只能從尚未問過的題庫題目中選（用題目 key），不能自己編題目。
  - 至少問滿 MIN_ASKED 題才可以提前結束；沒有可用的知識片段也不能提前結束。
  - 挑題時會參考 RAG 檢索到的知識片段，但題目文字一律用題庫原文。
  - 沒有金鑰、呼叫失敗或回傳不合格，就照題庫原本的順序問下一題。
急症判斷不在這裡（前端每答一題都會先跑問診規則）。
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

MIN_ASKED = 3          # 至少回答幾題（不含主訴）才可提前結束
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.05"))

SYSTEM = """你是骨科診所的問診助理，使用繁體中文。
依病患目前的回答與「知識片段」，從「待問題目」中挑一個最能幫助判斷問題類型的題目；
若資訊已足夠做初步分流，action 回 "done"。只能用待問題目的 key，不可自己編題目。
輸出 JSON：{"action": "ask" 或 "done", "key": "待問題目的key（done 時留空）", "reason": "15字內的原因"}。"""


def default_choice(remaining: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not remaining:
        return {"action": "done", "key": None, "reason": "", "source": "default"}
    return {"action": "ask", "key": remaining[0]["key"], "reason": "", "source": "default"}


def choose_next(part: str, answers: Dict[str, str], remaining: List[Dict[str, Any]],
                chunks: List[Dict[str, Any]], client: Any = None) -> Dict[str, Any]:
    fallback = default_choice(remaining)
    if not remaining or not (client or os.getenv("OPENAI_API_KEY")):
        return fallback
    usable = [c for c in chunks if (c.get("score") or 0) >= MIN_SCORE and c.get("content")]
    asked = sum(1 for k, v in answers.items() if k != "mainComplaint" and str(v).strip())
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI()
        given = "\n".join(f"- {('主訴' if k == 'mainComplaint' else k)}：{v}" for k, v in answers.items())
        pool = "\n".join(f"- {q['key']}：{q['question']}" for q in remaining)
        snippets = "\n".join(f"[{i}] {c['content']}" for i, c in enumerate(usable[:3], 1)) or "（無）"
        user = f"部位：{part}\n目前回答：\n{given}\n待問題目：\n{pool}\n知識片段：\n{snippets}"
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return fallback
    keys = {q["key"] for q in remaining}
    if data.get("action") == "done":
        if asked >= MIN_ASKED and usable:
            return {"action": "done", "key": None, "reason": str(data.get("reason", ""))[:30], "source": "gpt"}
        return fallback
    if data.get("action") == "ask" and data.get("key") in keys:
        return {"action": "ask", "key": data["key"], "reason": re.sub(r"\s+", "", str(data.get("reason", "")))[:30], "source": "gpt"}
    return fallback
