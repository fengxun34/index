import sqlite3
import json
import pickle
import numpy as np
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse  # 🌟 新增：讓 API 可以回傳漂亮網頁
from pydantic import BaseModel
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Optional, Dict, Any

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

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

class HistoryRequest(BaseModel):
    id_number: str

# 🌟 終極防彈：強制指定絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vector_store.db")
PKL_PATH = os.path.join(BASE_DIR, "vectorizer.pkl")

DEFAULT_SLOTS = ["早上 09:00 - 12:00", "下午 03:00 - 05:00", "晚上 06:00 - 09:00"]
DOCTOR_SCHEDULES = {
    "王醫師": ["早上 09:00 - 12:00", "下午 03:00 - 05:00"],
    "林醫師": ["下午 03:00 - 05:00", "晚上 06:00 - 09:00"],
    "陳醫師": ["早上 09:00 - 12:00"],
    "張醫師": ["晚上 06:00 - 09:00"],
    "吳醫師": ["早上 09:00 - 12:00", "晚上 06:00 - 09:00"],
    "謝醫師": ["下午 03:00 - 05:00", "晚上 06:00 - 09:00"],
    "杜醫師": ["早上 09:00 - 12:00"]
}

def load_sql_data():
    try:
        if not os.path.exists(PKL_PATH) or not os.path.exists(DB_PATH):
            print(f"❌ 找不到資料庫！請確認 {BASE_DIR} 裡面有沒有 vector_store.db")
            return [], [], None, None

        with open(PKL_PATH, "rb") as f:
            vec = pickle.load(f)
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("SELECT department, content, vector_json FROM medical_chunks")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            print("⚠ 警告：資料庫是空的！")
            return [], [], None, None
            
        depts = [r[0] for r in rows]
        conts = [r[1] for r in rows]
        matrix = np.array([json.loads(r[2]) for r in rows]).astype(np.float32)
        
        print(f"✅ 成功加載 {len(rows)} 筆醫療知識庫資料！")
        return depts, conts, vec, matrix
    except Exception as e:
        print(f"❌ 資料加載失敗: {e}")
        return [], [], None, None

departments, contents, vectorizer, doc_matrix = load_sql_data()

# ===== API 路由 =====

@app.post("/rag/answer")
def rag_answer(req: AnswerRequest):
    global departments, contents, vectorizer, doc_matrix
    if vectorizer is None:
        departments, contents, vectorizer, doc_matrix = load_sql_data()
    if vectorizer is None:
        return {"answer": "資料庫或向量模型未就緒", "retrieved_chunks": []}

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
        return {"answer": "無法判斷您的症狀，建議諮詢家醫科初診。", "retrieved_chunks": []}
    return {
        "answer": f"根據您的描述，建議掛【{hits[0]['department']}】",
        "retrieved_chunks": hits
    }

@app.post("/api/booking/confirm")
def confirm_booking(req: BookingRequest):
    allowed_slots = DOCTOR_SCHEDULES.get(req.doctor, DEFAULT_SLOTS)
    is_valid_slot = any(valid_time in req.slot for valid_time in allowed_slots)
    if not is_valid_slot:
        return {"status": "error", "message": f"驗證失敗：{req.doctor} 在該時段沒有看診！"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT,
                id_number TEXT,
                department TEXT,
                doctor TEXT,
                slot TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO appointments (patient_name, id_number, department, doctor, slot) VALUES (?, ?, ?, ?, ?)",
            (req.name, req.id_number, req.department, req.doctor, req.slot)
        )
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"掛號成功！已為您預約 {req.slot} {req.doctor}。"}
    except Exception as e:
        return {"status": "error", "message": f"存入資料庫失敗: {str(e)}"}

@app.post("/api/booking/history")
def get_booking_history(req: HistoryRequest):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT patient_name, department, doctor, slot, created_at 
            FROM appointments 
            WHERE id_number = ? 
            ORDER BY created_at DESC
        """, (req.id_number.upper(),))
        
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for r in rows:
            history.append({
                "name": r[0],
                "department": r[1],
                "doctor": r[2],
                "slot": r[3],
                "created_at": r[4]
            })
            
        return {"status": "success", "data": history}
    except Exception as e:
        return {"status": "error", "message": f"查詢失敗: {str(e)}"}

# 🌟 大升級：管理者專用，帶有漂亮網頁介面的 API
@app.get("/api/admin/all_bookings", response_class=HTMLResponse)
def get_all_bookings():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT patient_name, id_number, department, doctor, slot, created_at 
            FROM appointments 
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        
        # 產生表格的 HTML 內容
        tr_html = ""
        for r in rows:
            tr_html += f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td><td>{r[5]}</td></tr>"
            
        # 組合完整的網頁與 CSS
        html_content = f"""
        <!DOCTYPE html>
        <html lang="zh-Hant">
        <head>
            <meta charset="UTF-8">
            <title>診所掛號管理系統</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7f6; padding: 40px; color: #333; }}
                h2 {{ text-align: center; color: #2c3e50; font-size: 28px; margin-bottom: 5px; }}
                .subtitle {{ text-align: center; color: #7f8c8d; margin-bottom: 30px; }}
                .container {{ max-width: 1100px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 5px 15px rgba(0,0,0,0.08); }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                th, td {{ padding: 15px; text-align: left; border-bottom: 1px solid #e0e0e0; }}
                th {{ background-color: #007bff; color: white; font-weight: 600; font-size: 15px; }}
                th:first-child {{ border-top-left-radius: 8px; }}
                th:last-child {{ border-top-right-radius: 8px; }}
                tr:hover {{ background-color: #f8f9fa; }}
                td {{ font-size: 14px; color: #555; }}
                .empty {{ text-align: center; color: #999; padding: 40px; font-size: 16px; border: 2px dashed #ddd; border-radius: 8px; margin-top: 20px; }}
                .badge {{ background-color: #e3f2fd; color: #1976d2; padding: 4px 8px; border-radius: 4px; font-size: 13px; font-weight: bold; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>🏥 診所掛號總覽後台</h2>
                <div class="subtitle">目前系統內共有 <strong>{len(rows)}</strong> 筆掛號紀錄</div>
                
                {f'''
                <table>
                    <thead>
                        <tr>
                            <th>病患姓名</th>
                            <th>身分證字號</th>
                            <th>看診科別</th>
                            <th>看診醫師</th>
                            <th>預約時段</th>
                            <th>掛號建立時間</th>
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