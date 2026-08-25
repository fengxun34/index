import sqlite3
import json
import pickle
import numpy as np
import os
import re
import secrets
import html as html_lib
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse  # 讓 API 可以回傳漂亮網頁
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, field_validator
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# 骨科次專科清單（與 sql/schema.sql 的 CHECK 條件一致）
ALLOWED_DEPARTMENTS = [
    "脊椎外科", "運動醫學科", "關節重建科", "手外科",
    "足踝外科", "骨折創傷科", "骨質疏鬆症門診",
]

# 每個看診時段最多可掛號人數
MAX_PATIENTS_PER_SLOT = 4

# ===== 資料模型定義 =====

class AnswerRequest(BaseModel):
    question: str
    top_k: Optional[int] = 3
    metadata: Optional[Dict[str, Any]] = None

class BookingRequest(BaseModel):
    name: str
    id_number: str
    department: str
    doctor: str
    slot: str
    birth_date: Optional[str] = None
    phone: Optional[str] = None
    # 以下為 AI 問診紀錄，選填（略過問診直接掛號時不會有這些欄位）
    body_part: Optional[str] = None
    main_complaint: Optional[str] = None
    qa_answers: Optional[Dict[str, Any]] = None
    ai_suggestion: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("請填寫姓名")
        return v

    @field_validator("id_number")
    @classmethod
    def validate_id_number(cls, v):
        v = v.strip().upper()
        if not re.match(r"^[A-Z][12]\d{8}$", v):
            raise ValueError("身分證字號格式不正確，需為 1 個英文字母加 9 位數字（例如 A123456789）")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v):
        if v and not re.match(r"^09\d{8}$", v):
            raise ValueError("手機格式不正確，需為 09 開頭的 10 位數字")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if v not in ALLOWED_DEPARTMENTS:
            raise ValueError(f"科別不正確，需為以下其中一項：{'、'.join(ALLOWED_DEPARTMENTS)}")
        return v

class HistoryRequest(BaseModel):
    id_number: str

# ===== RAG 知識庫（本地 SQLite 向量庫，非病人個資） =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vector_store.db")
PKL_PATH = os.path.join(BASE_DIR, "vectorizer.pkl")

# 骨科次專科看診時段設定（用於驗證掛號時段是否合法）
DEFAULT_SLOTS = ["早上 09:00 - 12:00", "下午 03:00 - 05:00", "晚上 06:00 - 09:00"]
DOCTOR_SCHEDULES = {
    "高醫師": ["早上 09:00 - 12:00", "下午 03:00 - 05:00"],   # 脊椎外科
    "謝醫師": ["下午 03:00 - 05:00", "晚上 06:00 - 09:00"],   # 脊椎外科
    "曾醫師": ["早上 09:00 - 12:00", "晚上 06:00 - 09:00"],   # 運動醫學科
    "蔡醫師": ["下午 03:00 - 05:00"],                          # 運動醫學科
    "林醫師": ["早上 09:00 - 12:00", "下午 03:00 - 05:00"],   # 關節重建科
    "鍾醫師": ["晚上 06:00 - 09:00"],                          # 關節重建科
    "張醫師": ["早上 09:00 - 12:00"],                          # 手外科
    "王醫師": ["下午 03:00 - 05:00", "晚上 06:00 - 09:00"],   # 足踝外科
    "陳醫師": ["早上 09:00 - 12:00"],                          # 骨折創傷科
    "徐醫師": ["晚上 06:00 - 09:00"],                          # 骨折創傷科
    "吳醫師": ["早上 09:00 - 12:00", "下午 03:00 - 05:00"],   # 骨質疏鬆症門診
}

def load_sql_data():
    try:
        if not os.path.exists(PKL_PATH) or not os.path.exists(DB_PATH):
            print(f"❌ 找不到知識庫！請先執行 update.py 產生 {DB_PATH}")
            return [], [], None, None

        with open(PKL_PATH, "rb") as f:
            vec = pickle.load(f)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("SELECT department, content, vector_json FROM medical_chunks")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("⚠ 警告：知識庫是空的！")
            return [], [], None, None

        depts = [r[0] for r in rows]
        conts = [r[1] for r in rows]
        matrix = np.array([json.loads(r[2]) for r in rows]).astype(np.float32)

        print(f"✅ 成功加載 {len(rows)} 筆骨科醫療知識庫資料！")
        return depts, conts, vec, matrix
    except Exception as e:
        print(f"❌ 資料加載失敗: {e}")
        return [], [], None, None

departments, contents, vectorizer, doc_matrix = load_sql_data()

# ===== Supabase（病人資料 + 掛號紀錄） =====
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    print("✅ 已連線至 Supabase")
else:
    print("⚠️ 尚未設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，請參考 .env.example 設定，掛號功能暫時無法使用。")

# ===== 後台管理員登入驗證（帳密存在 Supabase 的 admin_users 表）=====
# 設定/修改密碼請用 set_admin_password.py，不需要手寫 SQL。
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="帳號或密碼錯誤",
        headers={"WWW-Authenticate": "Basic"},
    )
    if supabase is None:
        raise HTTPException(status_code=503, detail="Supabase 尚未設定")

    res = supabase.table("admin_users").select("password").eq("username", credentials.username).execute()
    if not res.data:
        raise unauthorized

    stored_password = res.data[0]["password"]
    if not secrets.compare_digest(credentials.password, stored_password):
        raise unauthorized

    return credentials.username

# ===== API 路由 =====

@app.post("/rag/answer")
def rag_answer(req: AnswerRequest):
    global departments, contents, vectorizer, doc_matrix
    if vectorizer is None:
        departments, contents, vectorizer, doc_matrix = load_sql_data()
    if vectorizer is None:
        return {"answer": "知識庫尚未就緒", "retrieved_chunks": []}

    query_vec = vectorizer.transform([req.question]).toarray().astype(np.float32)
    sims = cosine_similarity(query_vec, doc_matrix)[0]

    hits = []
    for i, s in enumerate(sims):
        if s > 0:
            hits.append({
                "department": departments[i],
                "content": contents[i],
                "score": float(s)
            })

    hits = sorted(hits, key=lambda x: x['score'], reverse=True)[:req.top_k]
    if not hits:
        return {"answer": "無法判斷您的症狀，建議先掛骨科門診由醫師評估。", "retrieved_chunks": []}
    return {
        "answer": f"根據您的描述，建議掛【骨科・{hits[0]['department']}】",
        "retrieved_chunks": hits
    }

@app.post("/api/booking/confirm")
def confirm_booking(req: BookingRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    allowed_slots = DOCTOR_SCHEDULES.get(req.doctor, DEFAULT_SLOTS)
    is_valid_slot = any(valid_time in req.slot for valid_time in allowed_slots)
    if not is_valid_slot:
        return {"status": "error", "message": f"驗證失敗：{req.doctor} 在該時段沒有看診！"}

    try:
        id_number = req.id_number.upper()

        # 以身分證字號為唯一鍵，寫入或更新病人基本資料
        patient_payload = {"id_number": id_number, "name": req.name}
        if req.birth_date:
            patient_payload["birth_date"] = req.birth_date.replace("/", "-")
        if req.phone:
            patient_payload["phone"] = req.phone

        patient_res = supabase.table("patients").upsert(
            patient_payload,
            on_conflict="id_number"
        ).execute()
        patient_id = patient_res.data[0]["id"]

        appointment_date, time_slot = (req.slot.split(" ", 1) + [""])[:2]
        time_slot = time_slot or req.slot

        existing = (
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("doctor", req.doctor)
            .eq("appointment_date", appointment_date)
            .eq("time_slot", time_slot)
            .eq("status", "confirmed")
            .execute()
        )
        if (existing.count or 0) >= MAX_PATIENTS_PER_SLOT:
            return {"status": "error", "message": f"{req.doctor} 在 {appointment_date} {time_slot} 已額滿，請選擇其他時段。"}

        appt_res = supabase.table("appointments").insert({
            "patient_id": patient_id,
            "department": req.department,
            "doctor": req.doctor,
            "appointment_date": appointment_date,
            "time_slot": time_slot,
        }).execute()
        appointment_id = appt_res.data[0]["id"] if appt_res.data else None

        # 若有 AI 問診過程（略過問診直接掛號時不會有），一併存下來供醫師看診前參考
        if req.body_part:
            supabase.table("triage_records").insert({
                "appointment_id": appointment_id,
                "patient_id": patient_id,
                "body_part": req.body_part,
                "main_complaint": req.main_complaint,
                "qa_answers": req.qa_answers,
                "recommended_department": req.department,
                "ai_suggestion": req.ai_suggestion,
            }).execute()

        return {"status": "success", "message": f"掛號成功！已為您預約 {req.slot} {req.doctor}。"}
    except Exception as e:
        return {"status": "error", "message": f"存入資料庫失敗: {str(e)}"}

@app.post("/api/booking/history")
def get_booking_history(req: HistoryRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    try:
        id_number = req.id_number.upper()
        patient_res = supabase.table("patients").select("id, name").eq("id_number", id_number).execute()
        if not patient_res.data:
            return {"status": "success", "data": []}

        patient = patient_res.data[0]
        appt_res = (
            supabase.table("appointments")
            .select("appointment_no, department, doctor, appointment_date, time_slot, created_at")
            .eq("patient_id", patient["id"])
            .order("created_at", desc=True)
            .execute()
        )

        history = [
            {
                "name": patient["name"],
                "appointment_no": a.get("appointment_no"),
                "department": a["department"],
                "doctor": a["doctor"],
                "slot": f"{a['appointment_date']} {a['time_slot']}",
                "created_at": a["created_at"],
            }
            for a in appt_res.data
        ]

        return {"status": "success", "data": history}
    except Exception as e:
        return {"status": "error", "message": f"查詢失敗: {str(e)}"}

# 管理者專用，帶有網頁介面的掛號總覽 API（需登入）
@app.get("/api/admin/all_bookings", response_class=HTMLResponse)
def get_all_bookings(admin_username: str = Depends(verify_admin)):
    if supabase is None:
        return HTMLResponse(content="<h2 style='text-align:center; color:red; margin-top:50px;'>Supabase 尚未設定</h2>")

    try:
        res = (
            supabase.table("appointments")
            .select("id, appointment_no, department, doctor, appointment_date, time_slot, created_at, patients(patient_no, name, id_number)")
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data or []

        triage_res = (
            supabase.table("triage_records")
            .select("appointment_id, body_part, main_complaint, qa_answers, ai_suggestion")
            .execute()
        )
        triage_by_appointment = {
            t["appointment_id"]: t for t in (triage_res.data or []) if t.get("appointment_id")
        }

        def esc(value):
            return html_lib.escape(str(value)) if value is not None else ""

        tr_html = ""
        for r in rows:
            patient = r.get("patients") or {}
            triage = triage_by_appointment.get(r.get("id"))

            if triage:
                qa_answers = triage.get("qa_answers") or {}
                qa_lines = "".join(
                    f"<li>{esc(k)}：{esc(v)}</li>"
                    for k, v in qa_answers.items()
                    if k != "mainComplaint"
                )
                triage_html = f"""
                    <details>
                        <summary>查看 AI 問診紀錄</summary>
                        <div class="triage-detail">
                            <p><strong>部位：</strong>{esc(triage.get('body_part'))}</p>
                            <p><strong>主訴：</strong>{esc(triage.get('main_complaint'))}</p>
                            {f'<ul>{qa_lines}</ul>' if qa_lines else ''}
                            <p><strong>AI 建議：</strong>{esc(triage.get('ai_suggestion'))}</p>
                        </div>
                    </details>
                """
            else:
                triage_html = "－"

            tr_html += (
                f"<tr><td>{esc(r.get('appointment_no'))}</td><td>{esc(patient.get('patient_no'))}</td>"
                f"<td>{esc(patient.get('name'))}</td><td>{esc(patient.get('id_number'))}</td>"
                f"<td>{esc(r['department'])}</td><td>{esc(r['doctor'])}</td>"
                f"<td>{esc(r['appointment_date'])} {esc(r['time_slot'])}</td><td>{esc(r['created_at'])}</td>"
                f"<td>{triage_html}</td></tr>"
            )

        html_content = f"""
        <!DOCTYPE html>
        <html lang="zh-Hant">
        <head>
            <meta charset="UTF-8">
            <title>骨科診所掛號管理系統</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7f6; padding: 40px; color: #333; }}
                h2 {{ text-align: center; color: #2c3e50; font-size: 28px; margin-bottom: 5px; }}
                .subtitle {{ text-align: center; color: #7f8c8d; margin-bottom: 30px; }}
                .container {{ max-width: 1100px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 5px 15px rgba(0,0,0,0.08); }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                th, td {{ padding: 15px; text-align: left; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
                th {{ background-color: #007bff; color: white; font-weight: 600; font-size: 15px; }}
                th:first-child {{ border-top-left-radius: 8px; }}
                th:last-child {{ border-top-right-radius: 8px; }}
                tr:hover {{ background-color: #f8f9fa; }}
                td {{ font-size: 14px; color: #555; }}
                .empty {{ text-align: center; color: #999; padding: 40px; font-size: 16px; border: 2px dashed #ddd; border-radius: 8px; margin-top: 20px; }}
                details summary {{ cursor: pointer; color: #007bff; font-weight: 600; }}
                .triage-detail {{ margin-top: 10px; padding: 10px 12px; background: #f4f7fb; border-radius: 8px; }}
                .triage-detail p {{ margin: 4px 0; }}
                .triage-detail ul {{ margin: 4px 0; padding-left: 18px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>🦴 骨科診所掛號總覽後台</h2>
                <div class="subtitle">目前系統內共有 <strong>{len(rows)}</strong> 筆掛號紀錄</div>

                {f'''
                <table>
                    <thead>
                        <tr>
                            <th>掛號序號</th>
                            <th>病歷號</th>
                            <th>病患姓名</th>
                            <th>身分證字號</th>
                            <th>骨科次專科</th>
                            <th>看診醫師</th>
                            <th>預約時段</th>
                            <th>掛號建立時間</th>
                            <th>AI 問診紀錄</th>
                        </tr>
                    </thead>
                    <tbody>
                        {tr_html}
                    </tbody>
                </table>
                ''' if rows else '<div class="empty">目前系統中尚無任何掛號紀錄。</div>'}
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)
    except Exception as e:
        return HTMLResponse(content=f"<h2 style='text-align:center; color:red; margin-top:50px;'>發生錯誤：{str(e)}</h2>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
