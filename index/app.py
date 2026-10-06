import sqlite3
import json
import pickle
import numpy as np
import os
import re
import secrets
import hashlib
import hmac
import base64
import html as html_lib
import httpx
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse  # 讓 API 可以回傳漂亮網頁
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, field_validator
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from supabase import create_client, Client
from postgrest.exceptions import APIError
from fastapi import Header
from set_admin_password import hash_password
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# ===== 連不到 Supabase 時的友善錯誤訊息 =====
# 常見原因：網路不通（學校／公司 Wi-Fi 擋連線）、Supabase 免費專案超過一週沒用被自動暫停。
# 不處理的話會噴一大串 Traceback，前端只看到「請確認後端是否已啟動」，很難找到真正原因。
DB_UNREACHABLE_MESSAGE = (
    "目前無法連線到 Supabase 資料庫。請確認：① 電腦網路是否正常（可改用手機熱點）；"
    "② Supabase 專案是否被暫停（登入 supabase.com，若顯示 paused 請按 Restore project）。"
)


def explain_db_error(e: Exception) -> Optional[str]:
    """把 Supabase 常見錯誤翻成看得懂的原因與處理方式；不認得的錯誤回傳 None。"""
    if isinstance(e, httpx.HTTPError):
        return DB_UNREACHABLE_MESSAGE
    if not isinstance(e, APIError):
        return None
    code = str(e.code or "")
    text = " ".join(str(x or "") for x in (e.message, e.details, e.hint)).lower()
    if ("clinic_questions" in text or "clinic_knowledge" in text) and (code in ("42P01", "PGRST205") or "does not exist" in text or "could not find the table" in text):
        return "Supabase 還沒建立診所自訂資料表：請到 SQL Editor 執行 sql/005_clinic_customization.sql。"
    if code in ("42P01", "PGRST205") or "does not exist" in text or "could not find the table" in text:
        return "Supabase 資料庫還沒建立資料表：請到 Supabase 專案的 SQL Editor，貼上並執行 sql/schema.sql。"
    if code in ("42703", "PGRST204") or "column" in text and ("not exist" in text or "could not find" in text):
        if "password_hash" in text:
            return "Supabase 還沒加上病患帳號欄位：請到 SQL Editor 執行 sql/004_patient_accounts.sql。"
        return "Supabase 資料表缺少欄位（可能是舊版資料表）：請到 SQL Editor 執行 sql/002_add_sequential_ids_and_triage.sql 與 sql/003_atomic_slot_capacity.sql。"
    if code in ("42883", "PGRST202") or "book_appointment_slot" in text:
        return "Supabase 缺少掛號用的資料庫函式：請到 SQL Editor 執行 sql/003_atomic_slot_capacity.sql。"
    if code in ("401", "403") or "invalid api key" in text or "jwt" in text or "permission denied" in text:
        return "Supabase 金鑰不正確或權限不足：請確認 .env 的 SUPABASE_SERVICE_ROLE_KEY 是 service_role key（不是 anon key），修改後重新啟動後端。"
    if code in ("540", "503", "502", "504") or "paused" in text or "json could not be generated" in text:
        return "Supabase 專案目前無法使用（可能已被暫停或正在恢復中）：請登入 supabase.com 確認專案狀態，若顯示 paused 請按 Restore project，恢復後等 1～3 分鐘再試。"
    return f"Supabase 回傳錯誤（{code}）：{e.message or e.details or '未知錯誤'}"


def db_error_message(prefix: str, e: Exception) -> str:
    return explain_db_error(e) or f"{prefix}: {str(e)}"


def _error_response(request: Request, message: str, status_code: int):
    if request.url.path.startswith("/api/admin"):
        return HTMLResponse(
            content=f"<h2 style='text-align:center; color:#c0392b; margin:50px auto; max-width:720px; line-height:1.6;'>{html_lib.escape(message)}</h2>",
            status_code=status_code,
        )
    return JSONResponse(status_code=status_code, content={"status": "error", "message": message})


@app.exception_handler(httpx.HTTPError)
async def handle_db_unreachable(request: Request, exc: httpx.HTTPError):
    print(f"⚠️ 無法連線到 Supabase：{exc}")
    return _error_response(request, DB_UNREACHABLE_MESSAGE, 503)


@app.exception_handler(APIError)
async def handle_supabase_error(request: Request, exc: APIError):
    message = explain_db_error(exc)
    print(f"⚠️ Supabase 回傳錯誤：{exc!r}\n→ {message}")
    return _error_response(request, message, 500)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    # 其他沒預料到的錯誤：後端視窗印出完整錯誤方便除錯，畫面上顯示錯誤類型而不是只有 Internal Server Error
    traceback.print_exc()
    return _error_response(request, f"系統發生錯誤（{type(exc).__name__}：{exc}），請把後端視窗的錯誤訊息提供給開發人員。", 500)


# ===== 小診所設定 =====
# 系統目標是社區型骨科診所：病人畫面只看到「問題類型＋推薦醫師」，不會出現大醫院的次專科名稱。
# 下面這 10 個類別是 AI 分流的「內部問題類型」，用來決定推薦哪位醫師、要不要建議轉診，
# 也存在 appointments.department 欄位（與 sql/schema.sql 的 CHECK 條件一致，不需要改資料庫）。
CLINIC_NAME = "骨科診所"

ALLOWED_DEPARTMENTS = [
    "脊椎外科", "運動醫學科", "關節重建科", "手外科",
    "足踝外科", "骨折創傷科", "骨質疏鬆症門診",
    "兒童骨科", "骨骼腫瘤科", "高壓氧治療中心",
]

# 內部問題類型 → 給病人看的白話名稱
CATEGORY_LABELS = {
    "脊椎外科": "脊椎（頸椎、腰椎）",
    "運動醫學科": "運動傷害",
    "關節重建科": "關節退化",
    "手外科": "手部（手腕、手指）",
    "足踝外科": "足踝",
    "骨折創傷科": "骨折、外傷",
    "骨質疏鬆症門診": "骨質疏鬆",
    "兒童骨科": "兒童骨骼",
    "骨骼腫瘤科": "疑似骨骼腫瘤",
    "高壓氧治療中心": "需高壓氧治療",
}

# 小診所通常無法處理、建議轉診到大醫院的問題類型（病人仍可選擇先讓診所醫師初步評估）
REFERRAL_CATEGORIES = {
    "骨骼腫瘤科": "骨骼腫瘤需要進一步影像與病理檢查，建議轉診至醫學中心骨科（骨腫瘤專科）。",
    "高壓氧治療中心": "本診所沒有高壓氧設備，建議轉診至設有高壓氧治療中心的醫院。",
}


def category_label(department: str) -> str:
    return CATEGORY_LABELS.get(department, department or "")

# 每個看診時段最多可掛號人數
MAX_PATIENTS_PER_SLOT = 4

# ===== 資料模型定義 =====

class AnswerRequest(BaseModel):
    question: str
    top_k: Optional[int] = 3
    metadata: Optional[Dict[str, Any]] = None
    explain: Optional[bool] = True  # 是否加上 GPT 判斷（聊天助理自己會判斷，會關掉）

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

# 查詢／取消／改期／修改資料都要「身分證字號＋生日」同時相符，
# 避免只知道別人的身分證字號就能看到或更動對方的掛號
class HistoryRequest(BaseModel):
    id_number: str
    birth_date: str

class CancelRequest(BaseModel):
    id_number: str
    birth_date: str
    appointment_no: int

class RescheduleRequest(BaseModel):
    id_number: str
    birth_date: str
    appointment_no: int
    new_slot: str
    # 選填：同一科別內改掛其他醫師（不給就維持原本的醫師）
    new_doctor: Optional[str] = None

class PatientUpdateRequest(BaseModel):
    id_number: str
    birth_date: str
    phone: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v):
        v = v.strip()
        if not re.match(r"^09\d{8}$", v):
            raise ValueError("手機格式不正確，需為 09 開頭的 10 位數字")
        return v

# ===== RAG 知識庫（本地 SQLite 向量庫，非病人個資） =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vector_store.db")
PKL_PATH = os.path.join(BASE_DIR, "vectorizer.pkl")

# ===== 診所醫師（擅長的問題類型可複選 + 每週固定看診時段） =====
# weekly 的 key 是星期幾（0=一, 1=二, 2=三, 3=四, 4=五, 5=六, 6=日），
# value 是當天有看診的時段代碼列表，取值只能是 "早上"／"下午"／"晚上"。
# departments 是這位醫師負責的「內部問題類型」，AI 分流後依此推薦醫師；
# specialty 是顯示給病人看的擅長項目。每個問題類型至少要有一位醫師負責。
DOCTORS = {
    "高醫師": {
        "specialty": "脊椎、骨質疏鬆",
        "departments": ["脊椎外科", "骨質疏鬆症門診", "骨骼腫瘤科", "高壓氧治療中心"],
        "weekly": {0: ["早上", "下午"], 2: ["早上", "下午"], 4: ["早上", "下午"]},
    },
    "曾醫師": {
        "specialty": "運動傷害、膝肩關節",
        "departments": ["運動醫學科", "關節重建科"],
        "weekly": {1: ["下午", "晚上"], 3: ["下午", "晚上"], 5: ["早上"]},
    },
    "林醫師": {
        "specialty": "手足部、骨折外傷、兒童骨科",
        "departments": ["手外科", "足踝外科", "骨折創傷科", "兒童骨科"],
        "weekly": {0: ["晚上"], 1: ["早上"], 2: ["晚上"], 3: ["早上"], 4: ["晚上"], 5: ["早上"]},
    },
}

# 醫師班表「例外」調整（請假／代診／臨時加開）。
# key 是 (醫師姓名, "YYYY-MM-DD")，value 是當天實際有看診的時段列表，
# 會覆蓋掉 DOCTORS 裡該醫師原本每週固定的班表。value 給空list [] 代表當天請假、完全不看診。
# 範例：
#   ("高醫師", "2026-09-21"): [],                     # 高醫師當天請假
#   ("高醫師", "2026-09-21"): ["早上", "下午", "晚上"], # 當天加開晚診
DOCTOR_SCHEDULE_OVERRIDES = {}

# 時段代碼 <-> 完整時段字串的對照（跟前端 index.html 的 timeSlots 保持一致）
SESSION_TIME_MAP = {
    "早上": "早上 09:00 - 12:00",
    "下午": "下午 03:00 - 05:00",
    "晚上": "晚上 06:00 - 09:00",
}
SESSION_BY_TIME_SLOT = {v: k for k, v in SESSION_TIME_MAP.items()}
SESSION_ORDER = ["早上", "下午", "晚上"]
SESSION_START_HOUR = {"早上": 9, "下午": 15, "晚上": 18}  # 用來判斷「今天」這個時段是否已經開始，開始了就不再排進今天
WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]

DEFAULT_SLOTS = list(SESSION_TIME_MAP.values())


def get_doctor_sessions(doctor: str, date_obj) -> list:
    """回傳某醫師在某一天實際有看診的時段代碼列表（優先看例外調整，沒有例外才用每週固定班表）。"""
    override_key = (doctor, date_obj.isoformat())
    if override_key in DOCTOR_SCHEDULE_OVERRIDES:
        return DOCTOR_SCHEDULE_OVERRIDES[override_key]
    info = DOCTORS.get(doctor)
    if not info:
        return []
    return info["weekly"].get(date_obj.weekday(), [])


def get_doctors_by_department(department: str) -> list:
    """回傳擅長科別包含該科的所有醫師（依 DOCTORS 定義順序）。"""
    return [name for name, info in DOCTORS.items() if department in info["departments"]]


def is_session_bookable_now(date_obj, session: str, now: datetime) -> bool:
    """今天以外的日期一律可掛；今天的時段如果已經開始看診，就不再提供掛號。"""
    if date_obj != now.date():
        return True
    start_hour = SESSION_START_HOUR.get(session, 0)
    return now.hour < start_hour


def slot_is_past(date_obj, session: str, now: Optional[datetime] = None) -> bool:
    """過去的日期、或今天已經開始看診的時段，後端一律不收（不能只靠前端不顯示）。"""
    now = now or datetime.now()
    return date_obj < now.date() or not is_session_bookable_now(date_obj, session, now)


def get_capacity_map(doctors: list, date_from, date_to) -> dict:
    """一次查詢範圍內所有醫師的已掛號數，回傳 {(doctor, date_str, time_slot): 已掛號人數}，避免逐格查詢資料庫。"""
    if supabase is None or not doctors:
        return {}
    res = (
        supabase.table("appointments")
        .select("doctor, appointment_date, time_slot")
        .in_("doctor", doctors)
        .gte("appointment_date", date_from.isoformat())
        .lte("appointment_date", date_to.isoformat())
        .eq("status", "confirmed")
        .execute()
    )
    counts = defaultdict(int)
    for row in (res.data or []):
        counts[(row["doctor"], row["appointment_date"], row["time_slot"])] += 1
    return counts

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
# 密碼以 PBKDF2-SHA256 雜湊存放（格式：pbkdf2_sha256$迭代次數$salt$hash），
# 資料庫外洩也不會直接曝露原始密碼。
security = HTTPBasic()

def verify_password(plain_password: str, stored_value: str) -> bool:
    try:
        algorithm, iterations, salt_hex, hash_hex = stored_value.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, int(iterations))
    return secrets.compare_digest(actual, expected)

# 後台登入失敗次數限制（記憶體內即可，示範用途不需要跨行程共享）：
# 同一帳號 15 分鐘內失敗滿 5 次就先鎖住，避免密碼被暴力破解。
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15
_failed_login_attempts = defaultdict(list)

def _is_locked_out(username: str) -> bool:
    window_start = datetime.utcnow() - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    recent = [t for t in _failed_login_attempts[username] if t > window_start]
    _failed_login_attempts[username] = recent
    return len(recent) >= MAX_LOGIN_ATTEMPTS

def _record_failed_login(username: str):
    _failed_login_attempts[username].append(datetime.utcnow())

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="帳號或密碼錯誤",
        headers={"WWW-Authenticate": "Basic"},
    )
    if supabase is None:
        raise HTTPException(status_code=503, detail="Supabase 尚未設定")

    if _is_locked_out(credentials.username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登入失敗次數過多，請 {LOGIN_LOCKOUT_MINUTES} 分鐘後再試。",
        )

    res = supabase.table("admin_users").select("password").eq("username", credentials.username).execute()
    if not res.data:
        _record_failed_login(credentials.username)
        raise unauthorized

    stored_password = res.data[0]["password"]
    if not verify_password(credentials.password, stored_password):
        _record_failed_login(credentials.username)
        raise unauthorized

    return credentials.username

# ===== 病人身分驗證（身分證字號＋生日）=====
# 同一身分證字號 15 分鐘內驗證失敗滿 5 次就先鎖住，避免有人用猜生日的方式硬試
MAX_VERIFY_ATTEMPTS = 5
VERIFY_LOCKOUT_MINUTES = 15
_failed_verify_attempts = defaultdict(list)
# 查無此人跟生日不符回同一句話，不透露「這個身分證字號有沒有掛過號」
VERIFY_FAILED_MESSAGE = "查無資料，或身分證字號與生日不符。"


def normalize_birth_date(value: str) -> Optional[str]:
    """把 1990/1/1、1990-01-01、1990.01.01 等寫法統一成 YYYY-MM-DD，格式不對回傳 None。"""
    v = (value or "").strip().replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(v, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def verify_patient_identity(id_number: str, birth_date: str, fields: str = "id"):
    """驗證身分證字號＋生日，成功回傳 (patient, None)，失敗回傳 (None, 錯誤訊息)。"""
    id_number = (id_number or "").strip().upper()
    window_start = datetime.utcnow() - timedelta(minutes=VERIFY_LOCKOUT_MINUTES)
    recent = [t for t in _failed_verify_attempts[id_number] if t > window_start]
    _failed_verify_attempts[id_number] = recent
    if len(recent) >= MAX_VERIFY_ATTEMPTS:
        return None, f"驗證失敗次數過多，請 {VERIFY_LOCKOUT_MINUTES} 分鐘後再試，或洽診所櫃台。"

    birth = normalize_birth_date(birth_date)
    if birth is None:
        return None, "生日格式不正確，請輸入 YYYY/MM/DD，例如 1990/01/01。"

    select_fields = fields if "birth_date" in fields else f"{fields}, birth_date"
    res = supabase.table("patients").select(select_fields).eq("id_number", id_number).execute()
    patient = res.data[0] if res.data else None
    if not patient or patient.get("birth_date") != birth:
        _failed_verify_attempts[id_number].append(datetime.utcnow())
        return None, VERIFY_FAILED_MESSAGE

    _failed_verify_attempts.pop(id_number, None)
    return patient, None


# ===== API 路由 =====

def _extract_advice(content: str) -> str:
    """從知識庫片段（update.py 組出的「部位：…。症狀：…。建議：…。看診醫師與時段：…」）取出建議那一段。"""
    m = re.search(r"建議：(.*?)。看診醫師", content or "")
    return m.group(1).strip() if m else ""


@app.post("/rag/answer")
def rag_answer(req: AnswerRequest):
    """檢索（TF-IDF）＋GPT 判斷（候選類型只來自檢索結果、理由經程式檢查）；GPT 不合格就只回原本的結果。"""
    result = _rag_retrieve(req)
    if req.explain and result.get("retrieved_chunks"):
        rule_dept = (req.metadata or {}).get("recommended_department")
        rule_dept = rule_dept if rule_dept in ALLOWED_DEPARTMENTS else None
        verdict = rag_generate.judge(req.question, result["retrieved_chunks"], rule_dept, category_label)
        if verdict:
            note = "AI 綜合判斷（僅限知識庫範圍）：較可能是【%s】。%s" % (verdict["label"], verdict["reason"])
            if rule_dept and not verdict["agrees_with_rule"]:
                note += "\n問診規則判斷為【%s】，兩者不同時請以醫師評估為準。" % category_label(rule_dept)
            result["answer"] += "\n" + note
            result["llm_triage"] = {k: verdict[k] for k in ("department", "label", "agrees_with_rule")}
    return result


def _rag_retrieve(req: AnswerRequest):
    if vectorizer is None:
        rebuild_rag_index()
    if vectorizer is None:
        return {"answer": "知識庫尚未就緒", "retrieved_chunks": [], "agreement": None}

    query_vec = vectorizer.transform([req.question]).toarray().astype(np.float32)
    sims = cosine_similarity(query_vec, doc_matrix)[0]
    ranked = sorted(((i, float(s)) for i, s in enumerate(sims) if s > 0), key=lambda x: x[1], reverse=True)
    # 不是小朋友的問題（部位不是「兒童」、描述也沒提到小孩），就不要拿兒童骨科的資料當依據，
    # 避免成人膝蓋痛被「青少年膝蓋痛」這類片段帶偏
    body_part = (req.metadata or {}).get("body_part")
    if body_part and body_part != "兒童" and not re.search(r"小孩|兒童|小朋友|孩子|青少年|寶寶|嬰兒", req.question):
        ranked = [(i, s) for i, s in ranked if departments[i] != "兒童骨科"]
    if not ranked:
        return {"answer": "無法判斷您的症狀，建議先掛骨科門診由醫師評估。", "retrieved_chunks": [], "agreement": None}

    top_k = max(1, req.top_k or 3)
    recommended = (req.metadata or {}).get("recommended_department")
    if recommended not in ALLOWED_DEPARTMENTS:
        recommended = None

    def to_chunk(i, s):
        return {
            "department": departments[i],
            "category_label": category_label(departments[i]),
            "content": contents[i],
            "source": sources[i] if i < len(sources) else CLINIC_BASE_SOURCE,
            "score": s,
            "supports_triage": recommended is not None and departments[i] == recommended,
        }

    hits = [to_chunk(i, s) for i, s in ranked[:top_k]]
    top_dept = departments[ranked[0][0]]

    # 沒有分流結果可以對照（例如單純查詢）：照舊直接回傳最相似的科別
    if recommended is None:
        return {
            "answer": f"根據您的描述，較可能是【{category_label(top_dept)}】相關問題，建議由骨科醫師評估。",
            "retrieved_chunks": hits,
            "agreement": None,
        }

    # 有分流結果：RAG 的角色是「幫分流結果找依據」，不是另外算一個答案跟它打架。
    # 找出知識庫中跟分流科別相符、相似度最高的片段；若不在前幾名也補進來，讓使用者看得到依據。
    supporting = next(((i, s) for i, s in ranked if departments[i] == recommended), None)
    if supporting and not any(h["supports_triage"] for h in hits):
        hits.append(to_chunk(*supporting))

    top_score = ranked[0][1]
    advice = _extract_advice(contents[supporting[0]]) if supporting else ""
    if top_dept == recommended:
        agreement = "agree"
        answer = f"知識庫也支持這個判斷：您的描述與【{category_label(recommended)}】的常見狀況最相符。"
    elif supporting and supporting[1] >= top_score * 0.5:
        agreement = "partial"
        answer = (f"知識庫也找到【{category_label(recommended)}】的相關資料；"
                  f"另外提示您的描述也與【{category_label(top_dept)}】有關，看診時可與醫師討論。")
    else:
        agreement = "differ"
        answer = (f"分流判斷為【{category_label(recommended)}】；"
                  f"知識庫另外提示可能與【{category_label(top_dept)}】有關，看診時可向醫師提出。")
    if advice:
        answer += f"\n參考建議：{advice}。"

    return {
        "answer": answer,
        "retrieved_chunks": hits,
        "agreement": agreement,
        "recommended_department": recommended,
    }

class NextQuestionRequest(BaseModel):
    body_part: str
    answers: Dict[str, str] = {}
    remaining_keys: List[str] = []


@app.post("/api/triage/next-question")
def triage_next_question(req: NextQuestionRequest):
    """問診追問：從診所題庫的「尚未問過」題目中，由 GPT 挑下一題或判斷可結束；沒有金鑰或失敗就照原順序。"""
    bank = get_clinic_questions().get(req.body_part) or []
    remaining = [q for q in bank if q["key"] in set(req.remaining_keys)]
    chunks = []
    if remaining and os.getenv("OPENAI_API_KEY"):
        question = "。".join([f"部位：{req.body_part}"] + [str(v) for v in req.answers.values() if str(v).strip()])
        try:
            chunks = _rag_retrieve(AnswerRequest(question=question, top_k=3, metadata={"body_part": req.body_part})).get("retrieved_chunks", [])
        except Exception:
            chunks = []
    choice = triage_questions.choose_next(req.body_part, req.answers, remaining, chunks)
    return {"status": "success", **choice}


@app.get("/api/clinic/info")
def get_clinic_info():
    """診所基本資料：醫師名單與擅長項目、問題類型白話名稱、需轉診的問題類型（前端顯示用）。"""
    return {
        "clinic_name": CLINIC_NAME,
        "doctors": [
            {"name": name, "specialty": info.get("specialty", ""), "departments": info["departments"]}
            for name, info in DOCTORS.items()
        ],
        "category_labels": CATEGORY_LABELS,
        "referral_categories": REFERRAL_CATEGORIES,
    }


@app.get("/api/schedule/clinic")
def get_clinic_schedule(days: int = 7):
    """回傳全診所所有醫師未來 N 天的班表與剩餘名額（直接掛號、指定醫師時使用）。"""
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    doctors = list(DOCTORS.keys())
    now = datetime.now()
    today = now.date()
    capacity = get_capacity_map(doctors, today, today + timedelta(days=days - 1))

    result_days = []
    for offset in range(days):
        check_date = today + timedelta(days=offset)
        date_str = check_date.isoformat()
        slots = []
        for session in SESSION_ORDER:
            for doc in doctors:
                if session not in get_doctor_sessions(doc, check_date) or not is_session_bookable_now(check_date, session, now):
                    continue
                time_slot = SESSION_TIME_MAP[session]
                used = capacity.get((doc, date_str, time_slot), 0)
                slots.append({
                    "doctor": doc,
                    "session": session,
                    "time_slot": time_slot,
                    "remaining": max(MAX_PATIENTS_PER_SLOT - used, 0),
                    "full": used >= MAX_PATIENTS_PER_SLOT,
                })
        result_days.append({"date": date_str, "weekday": WEEKDAY_LABELS[check_date.weekday()], "slots": slots})

    return {"status": "success", "doctors": doctors, "days": result_days}


@app.get("/api/schedule/department/{department}")
def get_department_schedule(department: str, days: int = 7):
    """回傳某科別未來 N 天（預設 7 天）的醫師班表，含每個時段目前剩餘可掛號名額。"""
    if department not in ALLOWED_DEPARTMENTS:
        raise HTTPException(status_code=400, detail=f"科別不正確：{department}")
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    doctors = get_doctors_by_department(department)
    if not doctors:
        return {"status": "success", "department": department, "doctors": [], "days": []}

    now = datetime.now()
    today = now.date()
    date_from = today
    date_to = today + timedelta(days=days - 1)
    capacity = get_capacity_map(doctors, date_from, date_to)

    result_days = []
    for offset in range(days):
        check_date = today + timedelta(days=offset)
        date_str = check_date.isoformat()
        slots = []
        for doc in doctors:
            for session in get_doctor_sessions(doc, check_date):
                if not is_session_bookable_now(check_date, session, now):
                    continue
                time_slot = SESSION_TIME_MAP[session]
                used = capacity.get((doc, date_str, time_slot), 0)
                slots.append({
                    "doctor": doc,
                    "session": session,
                    "time_slot": time_slot,
                    "remaining": max(MAX_PATIENTS_PER_SLOT - used, 0),
                    "full": used >= MAX_PATIENTS_PER_SLOT,
                })
        result_days.append({
            "date": date_str,
            "weekday": WEEKDAY_LABELS[check_date.weekday()],
            "slots": slots,
        })

    return {"status": "success", "department": department, "doctors": doctors, "days": result_days}


@app.get("/api/schedule/next-available")
def get_next_available_slot(department: str, doctor: Optional[str] = None, days: int = 30):
    """AI 自動安排門診用：找出某科別（可選指定醫師）最接近現在、且還有名額的時段。"""
    if department not in ALLOWED_DEPARTMENTS:
        raise HTTPException(status_code=400, detail=f"科別不正確：{department}")
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    candidates = [doctor] if doctor else get_doctors_by_department(department)
    candidates = [d for d in candidates if d in DOCTORS and department in DOCTORS[d]["departments"]]
    if not candidates:
        return {"status": "error", "message": "目前查無可掛號的醫師，請聯絡診所。"}

    now = datetime.now()
    today = now.date()
    date_to = today + timedelta(days=days - 1)
    capacity = get_capacity_map(candidates, today, date_to)

    for offset in range(days):
        check_date = today + timedelta(days=offset)
        date_str = check_date.isoformat()
        for session in SESSION_ORDER:
            if not is_session_bookable_now(check_date, session, now):
                continue
            for doc in candidates:
                if session not in get_doctor_sessions(doc, check_date):
                    continue
                time_slot = SESSION_TIME_MAP[session]
                used = capacity.get((doc, date_str, time_slot), 0)
                if used < MAX_PATIENTS_PER_SLOT:
                    return {
                        "status": "success",
                        "department": department,
                        "category_label": category_label(department),
                        "doctor": doc,
                        "doctor_specialty": DOCTORS[doc].get("specialty", ""),
                        "date": date_str,
                        "session": session,
                        "time_slot": time_slot,
                        "slot": f"{date_str} {time_slot}",
                        "remaining": MAX_PATIENTS_PER_SLOT - used,
                    }

    return {"status": "error", "message": f"未來{days}天內查無可掛號時段，請聯絡診所。"}


@app.post("/api/booking/confirm")
def confirm_booking(req: BookingRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    doctor_info = DOCTORS.get(req.doctor)
    if not doctor_info:
        return {"status": "error", "message": f"查無此醫師：{req.doctor}"}
    # 小診所情境：病人可以指定任何一位醫師。問題類型不在這位醫師的擅長項目時，
    # 改記成醫師的主要類型，讓 department 欄位永遠對得上醫師（後台與班表查詢才不會亂）
    department = req.department if req.department in doctor_info["departments"] else doctor_info["departments"][0]

    appointment_date, time_slot = (req.slot.split(" ", 1) + [""])[:2]
    time_slot = time_slot or req.slot
    try:
        appt_date_obj = datetime.strptime(appointment_date, "%Y-%m-%d").date()
    except ValueError:
        return {"status": "error", "message": "掛號時段格式不正確"}

    session = SESSION_BY_TIME_SLOT.get(time_slot)
    if session is None or session not in get_doctor_sessions(req.doctor, appt_date_obj):
        return {"status": "error", "message": f"驗證失敗：{req.doctor} 在 {appointment_date} 沒有看診！"}
    if slot_is_past(appt_date_obj, session):
        return {"status": "error", "message": "這個時段已經過了或已開始看診，請選擇其他時段。"}

    try:
        id_number = req.id_number.upper()

        # 以身分證字號為唯一鍵，寫入或更新病人基本資料
        patient_payload = {"id_number": id_number, "name": req.name}
        if req.birth_date:
            normalized_birth = normalize_birth_date(req.birth_date)
            if normalized_birth is None:
                return {"status": "error", "message": "生日格式不正確，請輸入 YYYY/MM/DD，例如 1990/01/01。"}
            patient_payload["birth_date"] = normalized_birth
        if req.phone:
            patient_payload["phone"] = req.phone

        # 已經掛過號的病人：生日必須跟第一次填的一樣，否則任何人都能用別人的身分證字號
        # 掛號並「改掉」對方的生日，再拿來查詢／取消對方的掛號
        existing = supabase.table("patients").select("birth_date").eq("id_number", id_number).execute()
        existing_birth = existing.data[0].get("birth_date") if existing.data else None
        if existing_birth and patient_payload.get("birth_date") != existing_birth:
            return {"status": "error", "message": "此身分證字號已有掛號資料，但生日與先前填寫的不符，請確認後再試，或洽診所櫃台。"}

        patient_res = supabase.table("patients").upsert(
            patient_payload,
            on_conflict="id_number"
        ).execute()
        patient_id = patient_res.data[0]["id"]

        # 同一個人同一天同一時段不能重複掛號（不論醫師）
        duplicate = (
            supabase.table("appointments").select("id", count="exact")
            .eq("patient_id", patient_id).eq("appointment_date", appointment_date)
            .eq("time_slot", time_slot).eq("status", "confirmed").execute()
        )
        if (duplicate.count or 0) > 0:
            return {"status": "error", "message": f"您在 {appointment_date} {time_slot} 已經有掛號了，請選擇其他時段或到「查詢掛號」修改。"}

        # 用資料庫端的原子操作（advisory lock）檢查容額並新增掛號，避免高併發下
        # 「先查詢再新增」這兩步不同交易，導致容額上限被打破（見 sql/003_atomic_slot_capacity.sql）
        try:
            appt_res = supabase.rpc("book_appointment_slot", {
                "p_patient_id": patient_id,
                "p_department": department,
                "p_doctor": req.doctor,
                "p_appointment_date": appointment_date,
                "p_time_slot": time_slot,
                "p_max_per_slot": MAX_PATIENTS_PER_SLOT,
            }).execute()
        except Exception as e:
            if "SLOT_FULL" in str(e):
                return {"status": "error", "message": f"{req.doctor} 在 {appointment_date} {time_slot} 已額滿，請選擇其他時段。"}
            raise

        appt_data = appt_res.data
        if isinstance(appt_data, list):
            appt_data = appt_data[0] if appt_data else None
        appointment_id = appt_data.get("id") if appt_data else None

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
        return {"status": "error", "message": db_error_message("存入資料庫失敗", e)}

def build_patient_history(patient: dict) -> dict:
    """組出病人的所有掛號紀錄、上次就診，以及最近一次 AI 問診（登入後帶入先前問答用）。"""
    appt_res = (
        supabase.table("appointments")
        .select("id, appointment_no, department, doctor, appointment_date, time_slot, status, created_at")
        .eq("patient_id", patient["id"])
        .order("created_at", desc=True)
        .execute()
    )

    # 一併帶出每次掛號當時的 AI 問診紀錄，讓病人回顧「上次看了什麼問題」
    triage_res = (
        supabase.table("triage_records")
        .select("appointment_id, body_part, main_complaint, qa_answers, ai_suggestion, created_at")
        .eq("patient_id", patient["id"])
        .execute()
    )
    triage_rows = triage_res.data or []
    triage_by_appointment = {t["appointment_id"]: t for t in triage_rows if t.get("appointment_id")}

    today_str = datetime.now().date().isoformat()
    history = []
    for a in appt_res.data or []:
        triage = triage_by_appointment.get(a["id"]) or {}
        history.append({
            "name": patient.get("name"),
            "appointment_no": a.get("appointment_no"),
            "department": a["department"],
            "category_label": category_label(a["department"]),
            "doctor": a["doctor"],
            "appointment_date": a["appointment_date"],
            "time_slot": a["time_slot"],
            "slot": f"{a['appointment_date']} {a['time_slot']}",
            "status": a.get("status", "confirmed"),
            "is_past": a["appointment_date"] < today_str,
            "created_at": a["created_at"],
            "body_part": triage.get("body_part"),
            "main_complaint": triage.get("main_complaint"),
            "ai_suggestion": triage.get("ai_suggestion"),
            "qa_answers": triage.get("qa_answers") or {},
        })

    # 上次就診：看診日期已經過去、且沒有取消的掛號裡，日期最近的一筆
    past_visits = [h for h in history if h["is_past"] and h["status"] != "cancelled"]
    def visit_order(h):
        session = SESSION_BY_TIME_SLOT.get(h["time_slot"])
        return (h["appointment_date"], SESSION_ORDER.index(session) if session in SESSION_ORDER else -1)
    last_visit = max(past_visits, key=visit_order, default=None)

    # 最近一次 AI 問診（不論是否已看診），登入後問診時可以「跟上次一樣」
    last_triage = None
    if triage_rows:
        t = max(triage_rows, key=lambda r: r.get("created_at") or "")
        appt = next((a for a in (appt_res.data or []) if a["id"] == t.get("appointment_id")), {})
        last_triage = {
            "body_part": t.get("body_part"),
            "main_complaint": t.get("main_complaint"),
            "qa_answers": t.get("qa_answers") or {},
            "department": appt.get("department"),
            "category_label": category_label(appt.get("department")),
            "doctor": appt.get("doctor"),
            "appointment_date": appt.get("appointment_date"),
        }

    return {"data": history, "last_visit": last_visit, "last_triage": last_triage}


@app.post("/api/booking/history")
def get_booking_history(req: HistoryRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    try:
        patient, error = verify_patient_identity(req.id_number, req.birth_date, "id, name")
        if error:
            return {"status": "error", "message": error}

        return {"status": "success", **build_patient_history(patient)}
    except Exception as e:
        return {"status": "error", "message": db_error_message("查詢失敗", e)}

@app.post("/api/booking/cancel")
def cancel_booking(req: CancelRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    try:
        patient, error = verify_patient_identity(req.id_number, req.birth_date)
        if error:
            return {"status": "error", "message": error}
        patient_id = patient["id"]

        appt_res = (
            supabase.table("appointments")
            .select("id, status, doctor, appointment_date, time_slot")
            .eq("patient_id", patient_id)
            .eq("appointment_no", req.appointment_no)
            .execute()
        )
        if not appt_res.data:
            return {"status": "error", "message": "查無此掛號紀錄，或身分證字號不符。"}
        appt = appt_res.data[0]
        if appt["status"] == "cancelled":
            return {"status": "error", "message": "此掛號已經是取消狀態。"}

        supabase.table("appointments").update({"status": "cancelled"}).eq("id", appt["id"]).execute()
        return {"status": "success", "message": f"已為您取消 {appt['appointment_date']} {appt['time_slot']} {appt['doctor']} 的掛號。"}
    except Exception as e:
        return {"status": "error", "message": db_error_message("取消掛號失敗", e)}

@app.post("/api/booking/reschedule")
def reschedule_booking(req: RescheduleRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    try:
        patient, error = verify_patient_identity(req.id_number, req.birth_date)
        if error:
            return {"status": "error", "message": error}
        patient_id = patient["id"]

        appt_res = (
            supabase.table("appointments")
            .select("id, patient_id, status, doctor, department")
            .eq("patient_id", patient_id)
            .eq("appointment_no", req.appointment_no)
            .execute()
        )
        if not appt_res.data:
            return {"status": "error", "message": "查無此掛號紀錄，或身分證字號不符。"}
        appt = appt_res.data[0]
        if appt["status"] != "confirmed":
            return {"status": "error", "message": "此掛號目前狀態無法改期。"}

        doctor = req.new_doctor or appt["doctor"]
        new_department = appt["department"]
        if doctor != appt["doctor"]:
            doctor_info = DOCTORS.get(doctor)
            if not doctor_info:
                return {"status": "error", "message": f"查無此醫師：{doctor}"}
            # 診所內任何醫師都可以改掛；問題類型不在新醫師擅長項目時，改記成新醫師的主要類型
            if new_department not in doctor_info["departments"]:
                new_department = doctor_info["departments"][0]
        new_date, new_time_slot = (req.new_slot.split(" ", 1) + [""])[:2]
        new_time_slot = new_time_slot or req.new_slot
        try:
            new_date_obj = datetime.strptime(new_date, "%Y-%m-%d").date()
        except ValueError:
            return {"status": "error", "message": "新的時段格式不正確"}

        new_session = SESSION_BY_TIME_SLOT.get(new_time_slot)
        if new_session is None or new_session not in get_doctor_sessions(doctor, new_date_obj):
            return {"status": "error", "message": f"驗證失敗：{doctor} 在該時段沒有看診！"}
        if slot_is_past(new_date_obj, new_session):
            return {"status": "error", "message": "這個時段已經過了或已開始看診，請選擇其他時段。"}
        same_time = (
            supabase.table("appointments").select("id", count="exact")
            .eq("patient_id", appt["patient_id"]).eq("appointment_date", new_date)
            .eq("time_slot", new_time_slot).eq("status", "confirmed").neq("id", appt["id"]).execute()
        )
        if (same_time.count or 0) > 0:
            return {"status": "error", "message": f"您在 {new_date} {new_time_slot} 已經有其他掛號了。"}

        existing = (
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("doctor", doctor)
            .eq("appointment_date", new_date)
            .eq("time_slot", new_time_slot)
            .eq("status", "confirmed")
            .execute()
        )
        if (existing.count or 0) >= MAX_PATIENTS_PER_SLOT:
            return {"status": "error", "message": f"{doctor} 在 {new_date} {new_time_slot} 已額滿，請選擇其他時段。"}

        supabase.table("appointments").update({
            "doctor": doctor,
            "department": new_department,
            "appointment_date": new_date,
            "time_slot": new_time_slot,
        }).eq("id", appt["id"]).execute()

        return {"status": "success", "message": f"已為您改期至 {new_date} {new_time_slot} {doctor}。"}
    except Exception as e:
        return {"status": "error", "message": db_error_message("改期失敗", e)}

# ===== 病患帳號（手機號碼或身分證字號＋密碼）=====
# 登入後：個人資料自動帶入、直接看到就診紀錄、問診時可帶入上次的回答。
# 登入憑證是後端簽章的 token（不另外建 session 表）；簽章裡包含密碼雜湊，改密碼後舊 token 自動失效。
SESSION_DAYS = 7
SESSION_SECRET = (os.environ.get("SESSION_SECRET")
                  or hashlib.sha256(f"patient-session|{SUPABASE_SERVICE_ROLE_KEY or 'dev'}".encode()).hexdigest())
ACCOUNT_FAILED_MESSAGE = "帳號或密碼錯誤。"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, password_hash: str) -> str:
    return _b64(hmac.new(SESSION_SECRET.encode(), f"{payload}|{password_hash}".encode(), hashlib.sha256).digest())


def issue_session_token(patient_id: str, password_hash: str) -> str:
    expires = int((datetime.utcnow() + timedelta(days=SESSION_DAYS)).timestamp())
    payload = f"{patient_id}.{expires}"
    return f"{_b64(payload.encode())}.{_sign(payload, password_hash)}"


def patient_from_token(authorization: Optional[str]):
    """驗證登入 token，回傳病人資料；無效或過期回傳 None。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload_b64, signature = authorization[7:].strip().split(".")
        payload = _unb64(payload_b64).decode()
        patient_id, expires = payload.rsplit(".", 1)
        if int(expires) < datetime.utcnow().timestamp():
            return None
    except Exception:
        return None
    res = supabase.table("patients").select("id, name, id_number, birth_date, phone, password_hash").eq("id", patient_id).execute()
    patient = res.data[0] if res.data else None
    if not patient or not patient.get("password_hash"):
        return None
    if not secrets.compare_digest(signature, _sign(payload, patient["password_hash"])):
        return None
    return patient


def account_profile(patient: dict) -> dict:
    birth = patient.get("birth_date") or ""
    return {
        "name": patient.get("name"),
        "id_number": patient.get("id_number"),
        "birth_date": birth.replace("-", "/"),
        "phone": patient.get("phone"),
    }


def _validate_password(password: str) -> Optional[str]:
    if len(password or "") < 6:
        return "密碼至少需要 6 個字元。"
    return None


def _phone_taken_by_other_account(phone: str, patient_id: Optional[str]) -> bool:
    res = supabase.table("patients").select("id, password_hash").eq("phone", phone).execute()
    return any(r.get("password_hash") and r["id"] != patient_id for r in (res.data or []))


class RegisterRequest(BaseModel):
    name: str
    id_number: str
    birth_date: str
    phone: str
    password: str

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
        v = v.strip()
        if not re.match(r"^09\d{8}$", v):
            raise ValueError("手機格式不正確，需為 09 開頭的 10 位數字")
        return v


class LoginRequest(BaseModel):
    account: str   # 手機號碼或身分證字號
    password: str


class ResetPasswordRequest(BaseModel):
    id_number: str
    birth_date: str
    new_password: str


@app.post("/api/account/register")
def register_account(req: RegisterRequest):
    """建立病患帳號。已經掛過號的病人需身分證＋生日相符；第一次來的病人會同時建立病人資料。"""
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}
    error = _validate_password(req.password)
    if error:
        return {"status": "error", "message": error}
    birth = normalize_birth_date(req.birth_date)
    if birth is None:
        return {"status": "error", "message": "生日格式不正確，請輸入 YYYY/MM/DD，例如 1990/01/01。"}
    try:
        existing = supabase.table("patients").select("id, birth_date, password_hash").eq("id_number", req.id_number).execute()
        if existing.data:
            patient = existing.data[0]
            if patient.get("password_hash"):
                return {"status": "error", "message": "這個身分證字號已經建立過帳號，請直接登入；忘記密碼可以用「忘記密碼」重設。"}
            if patient.get("birth_date") and patient["birth_date"] != birth:
                return {"status": "error", "message": "此身分證字號已有掛號資料，但生日與先前填寫的不符，請確認後再試，或洽診所櫃台。"}
            patient_id = patient["id"]
        else:
            patient_id = None
        if _phone_taken_by_other_account(req.phone, patient_id):
            return {"status": "error", "message": "這支手機已經綁定其他帳號；家人共用手機時，請改用身分證字號登入或註冊時填寫自己的手機。"}

        password_hash = hash_password(req.password)
        payload = {"name": req.name.strip(), "birth_date": birth, "phone": req.phone, "password_hash": password_hash}
        if patient_id:
            supabase.table("patients").update(payload).eq("id", patient_id).execute()
        else:
            created = supabase.table("patients").insert({"id_number": req.id_number, **payload}).execute()
            patient_id = created.data[0]["id"]

        patient = {"id": patient_id, "id_number": req.id_number, **payload}
        return {"status": "success", "message": "帳號建立完成！", "token": issue_session_token(patient_id, password_hash),
                "profile": account_profile(patient)}
    except Exception as e:
        return {"status": "error", "message": db_error_message("建立帳號失敗", e)}


@app.post("/api/account/login")
def login_account(req: LoginRequest):
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}
    account = (req.account or "").strip().upper()
    lock_key = f"patient:{account}"
    if _is_locked_out(lock_key):
        return {"status": "error", "message": f"登入失敗次數過多，請 {LOGIN_LOCKOUT_MINUTES} 分鐘後再試。"}
    try:
        field = "id_number" if re.match(r"^[A-Z][12]\d{8}$", account) else "phone"
        res = supabase.table("patients").select("id, name, id_number, birth_date, phone, password_hash").eq(field, account).execute()
        candidates = [p for p in (res.data or []) if p.get("password_hash")]
        patient = next((p for p in candidates if verify_password(req.password, p["password_hash"])), None)
        if not patient:
            _record_failed_login(lock_key)
            return {"status": "error", "message": ACCOUNT_FAILED_MESSAGE}
        _failed_login_attempts.pop(lock_key, None)
        return {"status": "success", "token": issue_session_token(patient["id"], patient["password_hash"]),
                "profile": account_profile(patient)}
    except Exception as e:
        return {"status": "error", "message": db_error_message("登入失敗", e)}


@app.get("/api/account/me")
def get_my_account(authorization: Optional[str] = Header(default=None)):
    """登入後取得個人資料、就診紀錄與最近一次問診（前端帶入用）。"""
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}
    patient = patient_from_token(authorization)
    if not patient:
        return JSONResponse(status_code=401, content={"status": "error", "message": "登入已過期，請重新登入。"})
    return {"status": "success", "profile": account_profile(patient), **build_patient_history(patient)}


@app.post("/api/account/reset-password")
def reset_password(req: ResetPasswordRequest):
    """忘記密碼：身分證字號＋生日相符即可設定新密碼（舊的登入 token 會一起失效）。"""
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}
    error = _validate_password(req.new_password)
    if error:
        return {"status": "error", "message": error}
    try:
        patient, error = verify_patient_identity(req.id_number, req.birth_date, "id, name, id_number, birth_date, phone, password_hash")
        if error:
            return {"status": "error", "message": error}
        if not patient.get("password_hash"):
            return {"status": "error", "message": "這個身分證字號還沒有建立帳號，請先註冊。"}
        password_hash = hash_password(req.new_password)
        supabase.table("patients").update({"password_hash": password_hash}).eq("id", patient["id"]).execute()
        patient["password_hash"] = password_hash
        return {"status": "success", "message": "密碼已重設，已為您登入。", "token": issue_session_token(patient["id"], password_hash),
                "profile": account_profile(patient)}
    except Exception as e:
        return {"status": "error", "message": db_error_message("重設密碼失敗", e)}


@app.post("/api/patient/update")
def update_patient_contact(req: PatientUpdateRequest):
    """修改預約聯絡資訊（手機）。"""
    if supabase is None:
        return {"status": "error", "message": "Supabase 尚未設定，請聯絡系統管理員。"}

    try:
        patient, error = verify_patient_identity(req.id_number, req.birth_date)
        if error:
            return {"status": "error", "message": error}
        if _phone_taken_by_other_account(req.phone, patient["id"]):
            return {"status": "error", "message": "這支手機已經綁定其他帳號，請改用其他手機號碼。"}

        supabase.table("patients").update({"phone": req.phone}).eq("id", patient["id"]).execute()
        return {"status": "success", "message": f"已將聯絡手機更新為 {req.phone}。"}
    except Exception as e:
        return {"status": "error", "message": db_error_message("修改資料失敗", e)}

# ===== 診所自訂：問診題目與診所知識（診所專屬 RAG）=====
# 問診題目：預設在 clinic_default_questions.json；診所在「診所設定頁」改過的部位存在 clinic_questions 表，
#           有自訂就用自訂、沒有就用預設，前端開啟時向 /api/clinic/questions 取得。
# 診所知識：存在 clinic_knowledge 表，跟 medical_sheet.csv 的基礎知識庫合併成同一個 RAG 索引。
CLINIC_BASE_SOURCE = "本地骨科知識庫"
with open(os.path.join(BASE_DIR, "clinic_default_questions.json"), encoding="utf-8") as _f:
    DEFAULT_QUESTIONS: Dict[str, List[Dict[str, Any]]] = json.load(_f)
BODY_PARTS = list(DEFAULT_QUESTIONS.keys())
sources: List[str] = []


def _clinic_rows(table: str, active_only: bool = False) -> list:
    """讀取診所自訂資料；還沒執行 sql/005_clinic_customization.sql 時回傳空列表，系統照常用預設值。"""
    if supabase is None:
        return []
    try:
        q = supabase.table(table).select("*")
        if active_only:
            q = q.eq("active", True)
        return q.execute().data or []
    except Exception as e:
        print(f"⚠️ 讀取 {table} 失敗（若尚未執行 sql/005_clinic_customization.sql 可忽略）：{e}")
        return []


def get_clinic_questions() -> Dict[str, List[Dict[str, Any]]]:
    merged = {part: [dict(q) for q in qs] for part, qs in DEFAULT_QUESTIONS.items()}
    custom: Dict[str, list] = defaultdict(list)
    for row in _clinic_rows("clinic_questions"):
        custom[row["body_part"]].append(row)
    for part, rows in custom.items():
        rows.sort(key=lambda r: r.get("sort_order") or 0)
        merged[part] = [{"key": r["q_key"], "question": r["question"], "options": r.get("options") or []} for r in rows]
    return merged


def clinic_knowledge_content(row: dict) -> str:
    # 跟 update.py 的格式一致，_extract_advice 才取得到「建議」那一段
    return (f"部位：{row.get('body_part') or '不限'}。症狀：{row.get('keywords') or ''}。"
            f"建議：{row.get('content') or ''}。看診醫師與時段：{CLINIC_NAME}。警告：{row.get('warning') or ''}")


def rebuild_rag_index():
    """重建 RAG 索引：基礎知識庫（vector_store.db）＋診所知識（clinic_knowledge 表）。"""
    global departments, contents, sources, vectorizer, doc_matrix
    base_depts, base_contents, base_vec, base_matrix = load_sql_data()
    clinic = _clinic_rows("clinic_knowledge", active_only=True)
    if not clinic:
        departments, contents, vectorizer, doc_matrix = base_depts, base_contents, base_vec, base_matrix
        sources = [CLINIC_BASE_SOURCE] * len(base_contents)
        return
    from sklearn.feature_extraction.text import TfidfVectorizer
    departments = list(base_depts) + [r.get("department") for r in clinic]
    contents = list(base_contents) + [clinic_knowledge_content(r) for r in clinic]
    sources = [CLINIC_BASE_SOURCE] * len(base_contents) + [f"診所知識：{r.get('title') or '未命名'}" for r in clinic]
    vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer="char_wb")
    doc_matrix = vectorizer.fit_transform(contents).toarray().astype(np.float32)
    print(f"✅ RAG 索引：基礎知識 {len(base_contents)} 筆＋診所知識 {len(clinic)} 筆")


@app.on_event("startup")
def _build_index_on_startup():
    rebuild_rag_index()


def rag_search(text: str, top_k: int = 3) -> list:
    if vectorizer is None or not text:
        return []
    sims = cosine_similarity(vectorizer.transform([text]).toarray().astype(np.float32), doc_matrix)[0]
    ranked = sorted(((i, float(s)) for i, s in enumerate(sims) if s > 0), key=lambda x: x[1], reverse=True)[:top_k]
    return [{"department": departments[i], "category_label": category_label(departments[i]), "content": contents[i],
             "source": sources[i] if i < len(sources) else CLINIC_BASE_SOURCE, "score": s} for i, s in ranked]


class QuestionItem(BaseModel):
    key: Optional[str] = None
    question: str
    options: Optional[List[str]] = None


class QuestionSetRequest(BaseModel):
    questions: List[QuestionItem]


class KnowledgeRequest(BaseModel):
    id: Optional[str] = None
    title: str
    body_part: Optional[str] = None
    department: Optional[str] = None
    keywords: Optional[str] = ""
    content: str
    warning: Optional[str] = ""
    active: bool = True


@app.get("/api/clinic/questions")
def public_clinic_questions():
    return {"status": "success", "questions": get_clinic_questions()}


@app.get("/api/admin/clinic/data")
def admin_clinic_data(admin_username: str = Depends(verify_admin)):
    custom_parts = sorted({r["body_part"] for r in _clinic_rows("clinic_questions")})
    knowledge = sorted(_clinic_rows("clinic_knowledge"), key=lambda r: r.get("created_at") or "")
    return {
        "status": "success",
        "body_parts": BODY_PARTS,
        "questions": get_clinic_questions(),
        "custom_parts": custom_parts,
        "knowledge": knowledge,
        "categories": CATEGORY_LABELS,
    }


@app.put("/api/admin/clinic/questions/{body_part}")
def admin_save_questions(body_part: str, req: QuestionSetRequest, admin_username: str = Depends(verify_admin)):
    if body_part not in BODY_PARTS:
        raise HTTPException(status_code=400, detail=f"部位不正確：{body_part}")
    items = [q for q in req.questions if q.question.strip()]
    if not items:
        return {"status": "error", "message": "至少要有一題。"}
    used = set()
    rows = []
    for idx, q in enumerate(items):
        key = (q.key or "").strip() or f"q{idx + 1}"
        while key in used:
            key = f"{key}_{idx + 1}"
        used.add(key)
        options = [o.strip() for o in (q.options or []) if o.strip()]
        rows.append({"body_part": body_part, "sort_order": idx, "q_key": key, "question": q.question.strip(), "options": options})
    supabase.table("clinic_questions").delete().eq("body_part", body_part).execute()
    supabase.table("clinic_questions").insert(rows).execute()
    return {"status": "success", "message": f"已儲存「{body_part}」的 {len(rows)} 題問診題目。"}


@app.delete("/api/admin/clinic/questions/{body_part}")
def admin_reset_questions(body_part: str, admin_username: str = Depends(verify_admin)):
    supabase.table("clinic_questions").delete().eq("body_part", body_part).execute()
    return {"status": "success", "message": f"「{body_part}」已恢復成系統預設題目。"}


@app.post("/api/admin/clinic/knowledge")
def admin_save_knowledge(req: KnowledgeRequest, admin_username: str = Depends(verify_admin)):
    if req.department and req.department not in ALLOWED_DEPARTMENTS:
        return {"status": "error", "message": "問題類型不正確。"}
    if not req.title.strip() or not req.content.strip():
        return {"status": "error", "message": "標題與內容都要填寫。"}
    payload = {
        "title": req.title.strip(), "body_part": (req.body_part or "").strip() or None, "department": req.department or None,
        "keywords": (req.keywords or "").strip(), "content": req.content.strip(), "warning": (req.warning or "").strip(),
        "active": req.active, "updated_at": datetime.utcnow().isoformat(),
    }
    if req.id:
        supabase.table("clinic_knowledge").update(payload).eq("id", req.id).execute()
    else:
        supabase.table("clinic_knowledge").insert(payload).execute()
    rebuild_rag_index()
    return {"status": "success", "message": "已儲存，AI 知識庫已更新。"}


@app.delete("/api/admin/clinic/knowledge/{knowledge_id}")
def admin_delete_knowledge(knowledge_id: str, admin_username: str = Depends(verify_admin)):
    supabase.table("clinic_knowledge").delete().eq("id", knowledge_id).execute()
    rebuild_rag_index()
    return {"status": "success", "message": "已刪除，AI 知識庫已更新。"}


def _age(birth: Optional[str], on_date: str) -> Optional[int]:
    try:
        b = datetime.strptime(birth, "%Y-%m-%d").date()
        d = datetime.strptime(on_date, "%Y-%m-%d").date()
        return d.year - b.year - ((d.month, d.day) < (b.month, b.day))
    except Exception:
        return None


@app.get("/api/admin/today-data")
def admin_today_data(date: Optional[str] = None, doctor: Optional[str] = None, admin_username: str = Depends(verify_admin)):
    """醫師看診前摘要：某一天（預設今天）的看診清單，含問診問答、過去看診紀錄與相關的診所知識。"""
    date = date or datetime.now().date().isoformat()
    appts = [a for a in (supabase.table("appointments")
                         .select("id, appointment_no, patient_id, department, doctor, appointment_date, time_slot, status")
                         .eq("appointment_date", date).execute().data or [])
             if a.get("status") != "cancelled" and (not doctor or a["doctor"] == doctor)]
    patient_ids = list({a["patient_id"] for a in appts})
    patients = {p["id"]: p for p in (supabase.table("patients").select("id, patient_no, name, birth_date")
                                     .in_("id", patient_ids).execute().data or [])} if patient_ids else {}
    triage = {t["appointment_id"]: t for t in (supabase.table("triage_records")
                                               .select("appointment_id, body_part, main_complaint, qa_answers, ai_suggestion")
                                               .in_("appointment_id", [a["id"] for a in appts]).execute().data or [])} if appts else {}
    all_visits = (supabase.table("appointments").select("patient_id, appointment_date, doctor, department, status")
                  .in_("patient_id", patient_ids).execute().data or []) if patient_ids else []
    questions = get_clinic_questions()

    items = []
    for a in appts:
        p = patients.get(a["patient_id"], {})
        t = triage.get(a["id"]) or {}
        past = sorted([v for v in all_visits if v["patient_id"] == a["patient_id"] and v["appointment_date"] < date
                       and v.get("status") != "cancelled"], key=lambda v: v["appointment_date"])
        # 題目文字：先找目前的題目；病人回答後診所又改過題目的話，再用系統預設題目對應，都找不到才顯示代碼
        part = t.get("body_part") or ""
        q_text = {q["key"]: q["question"] for q in DEFAULT_QUESTIONS.get(part, [])}
        q_text.update({q["key"]: q["question"] for q in questions.get(part, [])})
        qa = [{"question": q_text.get(k, k), "answer": v} for k, v in (t.get("qa_answers") or {}).items() if k != "mainComplaint"]
        search_text = " ".join(filter(None, [t.get("body_part"), t.get("main_complaint")] + [x["answer"] for x in qa]))
        hits = rag_search(search_text, top_k=3) if search_text else []
        session = SESSION_BY_TIME_SLOT.get(a["time_slot"])
        items.append({
            "appointment_no": a.get("appointment_no"),
            "doctor": a["doctor"],
            "time_slot": a["time_slot"],
            "session_order": SESSION_ORDER.index(session) if session in SESSION_ORDER else 9,
            "patient_name": p.get("name"),
            "patient_no": p.get("patient_no"),
            "age": _age(p.get("birth_date"), date),
            "visit_type": "複診" if past else "初診",
            "past_visit_count": len(past),
            "last_visit": ({"date": past[-1]["appointment_date"], "doctor": past[-1]["doctor"],
                            "category_label": category_label(past[-1]["department"])} if past else None),
            "category_label": category_label(a["department"]),
            "body_part": t.get("body_part"),
            "main_complaint": t.get("main_complaint"),
            "qa": qa,
            "ai_suggestion": t.get("ai_suggestion"),
            "knowledge": [{"source": h["source"], "advice": _extract_advice(h["content"]),
                           "warning": (re.search(r"警告：(.*)$", h["content"]) or [None, ""])[1]} for h in hits],
        })
    items.sort(key=lambda x: (x["session_order"], x["doctor"], x["appointment_no"] or 0))
    return {"status": "success", "date": date, "doctors": list(DOCTORS.keys()), "items": items}


def _admin_page(filename: str) -> HTMLResponse:
    with open(os.path.join(BASE_DIR, filename), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/admin/today", response_class=HTMLResponse)
def admin_today_page(admin_username: str = Depends(verify_admin)):
    return _admin_page("admin_today.html")


@app.get("/api/admin/clinic", response_class=HTMLResponse)
def admin_clinic_page(admin_username: str = Depends(verify_admin)):
    return _admin_page("admin_clinic.html")


# ===== 聊天式掛號助理（規則版代理人，邏輯在 agent.py）=====
# 代理人的「工具」直接沿用既有的後端功能，身分驗證、防超賣、班表檢查都照常把關。
import agent as chat_agent
import agent_llm
import rag_generate
import triage_questions
from pydantic import ValidationError


def _call_tool(fn, model_cls, **kwargs):
    try:
        return fn(model_cls(**kwargs))
    except ValidationError as e:
        return {"status": "error", "message": "；".join(err["msg"].replace("Value error, ", "") for err in e.errors())}


AGENT_TOOLS = chat_agent.AgentTools(
    questions=lambda part: get_clinic_questions().get(part) or get_clinic_questions().get("未明", []),
    next_available=lambda department, doctor: get_next_available_slot(department, doctor),
    clinic_schedule=lambda days: get_clinic_schedule(days),
    book=lambda payload: _call_tool(confirm_booking, BookingRequest, **payload),
    history=lambda id_number, birth: _call_tool(get_booking_history, HistoryRequest, id_number=id_number, birth_date=birth),
    cancel=lambda id_number, birth, no: _call_tool(cancel_booking, CancelRequest, id_number=id_number, birth_date=birth, appointment_no=no),
    reschedule=lambda id_number, birth, no, new_slot, new_doctor: _call_tool(
        reschedule_booking, RescheduleRequest, id_number=id_number, birth_date=birth, appointment_no=no,
        new_slot=new_slot, new_doctor=new_doctor),
    rag=lambda question, metadata: rag_answer(AnswerRequest(question=question, top_k=3, metadata=metadata, explain=False)),
    doctors=DOCTORS,
    category_labels=CATEGORY_LABELS,
    referral_categories=REFERRAL_CATEGORIES,
)


class AgentChatRequest(BaseModel):
    message: str = ""
    state: Optional[Dict[str, Any]] = None


@app.post("/api/agent/chat")
def agent_chat(req: AgentChatRequest, authorization: Optional[str] = Header(default=None)):
    """聊天式掛號助理：前端每一輪送「使用者這句話＋上一輪的對話狀態」，回傳助理的回覆、新狀態與快捷回覆。"""
    profile = None
    if authorization and supabase is not None:
        patient = patient_from_token(authorization)
        profile = account_profile(patient) if patient else None
    # 有設定 OPENAI_API_KEY 就用 LLM 版；沒設定或呼叫失敗時退回規則版（兩種版本的對話狀態不通用，切換時重新開始）
    if agent_llm.llm_enabled():
        state = req.state if (req.state or {}).get("mode") == "llm" else None
        try:
            return agent_llm.handle_turn_llm(state, req.message, AGENT_TOOLS, profile=profile)
        except Exception:
            traceback.print_exc()
    state = None if (req.state or {}).get("mode") == "llm" else req.state
    return chat_agent.handle_turn(state, req.message, AGENT_TOOLS, profile=profile)


# 管理者專用，帶有網頁介面的掛號總覽 API（需登入）
@app.get("/api/admin/all_bookings", response_class=HTMLResponse)
def get_all_bookings(admin_username: str = Depends(verify_admin)):
    if supabase is None:
        return HTMLResponse(content="<h2 style='text-align:center; color:red; margin-top:50px;'>Supabase 尚未設定</h2>")

    try:
        res = (
            supabase.table("appointments")
            .select("id, appointment_no, department, doctor, appointment_date, time_slot, status, created_at, patients(patient_no, name, id_number)")
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data or []

        # 累計看診次數：以身分證字號為準，取消的掛號不算一次看診
        visit_counts: Dict[str, int] = {}
        for r in rows:
            if r.get("status") == "cancelled":
                continue
            id_number = (r.get("patients") or {}).get("id_number")
            if id_number:
                visit_counts[id_number] = visit_counts.get(id_number, 0) + 1

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
            is_cancelled = r.get("status") == "cancelled"
            status_html = (
                '<span style="color:#c0392b;font-weight:600;">已取消</span>'
                if is_cancelled
                else '<span style="color:#27ae60;font-weight:600;">正常</span>'
            )
            visit_count = visit_counts.get(patient.get("id_number"), 0)

            if triage:
                qa_answers = triage.get("qa_answers") or {}
                qa_lines = "".join(
                    f"<li>{esc(k)}：{esc(v)}</li>"
                    for k, v in qa_answers.items()
                    if k != "mainComplaint"
                )
                # 醫師看診前不用點開就能先掃過重點：部位＋主訴的一行摘要，
                # 完整問答與 AI 建議還是收在展開區塊裡，需要細節再點開
                summary_text = f"{triage.get('body_part') or '未提供部位'}／{triage.get('main_complaint') or '未提供主訴'}"
                if len(summary_text) > 40:
                    summary_text = summary_text[:40] + "…"
                triage_html = f"""
                    <div class="triage-summary">📋 {esc(summary_text)}</div>
                    <details>
                        <summary>查看完整 AI 問診紀錄</summary>
                        <div class="triage-detail">
                            <p><strong>部位：</strong>{esc(triage.get('body_part'))}</p>
                            <p><strong>主訴：</strong>{esc(triage.get('main_complaint'))}</p>
                            {f'<ul>{qa_lines}</ul>' if qa_lines else ''}
                            <p><strong>AI 建議：</strong>{esc(triage.get('ai_suggestion'))}</p>
                        </div>
                    </details>
                """
            else:
                triage_html = '<span style="color:#999;">－（略過問診直接掛號）</span>'

            tr_html += (
                f"<tr><td>{esc(r.get('appointment_no'))}</td><td>{esc(patient.get('patient_no'))}</td>"
                f"<td>{esc(patient.get('name'))}</td><td>{esc(patient.get('id_number'))}</td>"
                f"<td>{esc(category_label(r['department']))}</td><td>{esc(r['doctor'])}</td>"
                f"<td>{esc(r['appointment_date'])} {esc(r['time_slot'])}</td><td>{esc(r['created_at'])}</td>"
                f"<td>{status_html}</td><td style=\"text-align:center;\">{visit_count}</td>"
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
                .admin-nav {{ max-width: 1100px; margin: 0 auto 18px; display: flex; gap: 8px; flex-wrap: wrap; }}
                .admin-nav a {{ padding: 8px 14px; border-radius: 999px; background: #fff; border: 1px solid #e2e8f0; color: #2563eb; text-decoration: none; font-weight: 700; font-size: 14px; }}
                .admin-nav a.active {{ background: #2563eb; color: #fff; border-color: transparent; }}
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
                .triage-summary {{ font-size: 13.5px; color: #444; margin-bottom: 6px; font-weight: 600; }}
            </style>
        </head>
        <body>
            <nav class="admin-nav">
                <a href="/api/admin/all_bookings" class="active">📋 掛號總覽</a>
                <a href="/api/admin/today">🩺 今日看診清單</a>
                <a href="/api/admin/clinic">⚙️ 診所設定（問診題目／知識庫）</a>
            </nav>
            <div class="container">
                <h2>🦴 {esc(CLINIC_NAME)}掛號總覽後台</h2>
                <div class="subtitle">目前系統內共有 <strong>{len(rows)}</strong> 筆掛號紀錄</div>

                {f'''
                <table>
                    <thead>
                        <tr>
                            <th>掛號序號</th>
                            <th>病歷號</th>
                            <th>病患姓名</th>
                            <th>身分證字號</th>
                            <th>問題類型</th>
                            <th>看診醫師</th>
                            <th>預約時段</th>
                            <th>掛號建立時間</th>
                            <th>狀態</th>
                            <th>累計看診次數</th>
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
        return HTMLResponse(content=f"<h2 style='text-align:center; color:red; margin-top:50px;'>{html_lib.escape(db_error_message('發生錯誤', e))}</h2>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

# LINE 通知模組（未設定 LINE 時不影響既有掛號）
import line_bot
line_bot.install(app, lambda: supabase, patient_from_token)
