"""題庫內挑題；腰痛示範必問題目不交由 LLM 略過。"""
import json
import os
from clinical_demo import POLICIES, next_choice

def choose_next(body_part, answers, remaining, chunks, client=None):
    if body_part in POLICIES:
        return next_choice(answers, remaining, body_part)
    fallback = {"action": "ask", "key": remaining[0]["key"], "source": "default"} if remaining else {"action": "done", "source": "default"}
    count = sum(1 for k, v in answers.items() if k != "mainComplaint" and str(v).strip() and v != "（略過）")
    if count >= 4 or not remaining:
        return {"action": "done", "source": "default"}
    if not client and not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(timeout=5, max_retries=0)
        result = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0,
            response_format={"type": "json_object"}, messages=[
                {"role": "system", "content": "只從 remaining 題庫選下一題。輸出 JSON action=ask,key=題目代碼；資訊足夠時 action=done。不可編造題目。"},
                {"role": "user", "content": json.dumps({"body_part": body_part, "answers": answers, "remaining": remaining, "knowledge": chunks}, ensure_ascii=False)}])
        choice = json.loads(result.choices[0].message.content)
        if choice.get("action") == "done" and count >= 2 and chunks:
            return {"action": "done", "source": "gpt"}
        if choice.get("action") == "ask" and choice.get("key") in {q["key"] for q in remaining}:
            return {"action": "ask", "key": choice["key"], "source": "gpt"}
    except Exception:
        pass
    return fallback
