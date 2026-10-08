"""一鍵環境檢查：後端跑不起來、畫面一直出錯時，先執行這支程式找原因。

用法（在專案資料夾裡）：
    python check_setup.py

會依序檢查：Python 版本 → 套件 → .env 設定 → 網路／DNS → Supabase 金鑰 →
資料表與掛號函式 → 管理員帳號 → RAG 知識庫 → 後端是否已啟動。
每一項都會顯示 ✅／❌ 與建議的處理方式；不會修改資料庫、不會印出金鑰內容。
"""

import base64
import json
import os
import socket
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
problems = []


def ok(msg):
    print(f"  ✅ {msg}")


def bad(msg, fix):
    print(f"  ❌ {msg}")
    print(f"     👉 {fix}")
    problems.append(msg)


def warn(msg):
    print(f"  ⚠️ {msg}")


def section(title):
    print(f"\n【{title}】")


def main():
    print("=" * 56)
    print(" 醫語搞定-AI智慧語音掛號助理：環境檢查")
    print("=" * 56)

    # 1. Python
    section("1. Python")
    v = sys.version_info
    if v >= (3, 9):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python 版本太舊（{v.major}.{v.minor}）", "請安裝 Python 3.11")

    # 2. 套件
    section("2. 套件")
    missing = []
    for mod, pkg in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("numpy", "numpy"), ("sklearn", "scikit-learn"),
                     ("pandas", "pandas"), ("supabase", "supabase"), ("dotenv", "python-dotenv"), ("httpx", "httpx")]:
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        bad(f"缺少套件：{', '.join(missing)}", "執行：pip install -r requirements_true_rag.txt")
        print("\n（套件沒裝好，後面的檢查無法進行）")
        return
    ok("需要的套件都已安裝")

    # 3. .env
    section("3. .env 設定")
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        bad("找不到 .env 檔案", f"把 .env 放到這個資料夾：{BASE_DIR}（格式參考 .env.example）")
        return
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)
    url = (os.environ.get("SUPABASE_URL") or "").strip().strip('"').rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip().strip('"')
    if not url or "your-project-ref" in url:
        bad("SUPABASE_URL 沒有填", "到 Supabase → Project Settings → API 複製 Project URL，填到 .env")
        return
    if not url.startswith("https://") or ".supabase.co" not in url:
        bad(f"SUPABASE_URL 格式看起來不對（{url[:30]}…）", "應該長得像 https://xxxxxxxxxxxx.supabase.co")
        return
    ok(f"SUPABASE_URL：{url.split('//')[1][:4]}****.supabase.co")
    if not key or key == "your-service-role-key":
        bad("SUPABASE_SERVICE_ROLE_KEY 沒有填", "到 Supabase → Project Settings → API 複製 service_role key，填到 .env")
        return
    role = None
    if key.startswith("eyJ") and key.count(".") == 2:
        try:
            payload = key.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
        except Exception:
            pass
    if role == "anon":
        bad("SUPABASE_SERVICE_ROLE_KEY 填成了 anon key", "要填 service_role key（Project Settings → API 裡標示 service_role／secret 的那一把）")
    elif role == "service_role" or key.startswith("sb_secret_"):
        ok("金鑰類型是 service_role")
    else:
        warn("無法判斷金鑰類型，繼續往下檢查")

    # 4. 網路
    section("4. 網路連線")
    host = url.split("//")[1].split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
        ok("找得到 Supabase 伺服器（DNS 正常）")
    except OSError:
        bad("找不到 Supabase 伺服器（DNS 失敗，就是 getaddrinfo failed）",
            "① 登入 supabase.com，專案若顯示 paused 請按 Restore project；② 換手機熱點再試；③ 確認 .env 的網址沒打錯")
        return
    import httpx
    try:
        r = httpx.get(f"{url}/rest/v1/", headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)
        if r.status_code in (401, 403):
            bad(f"金鑰被 Supabase 拒絕（HTTP {r.status_code}）", "重新到 Project Settings → API 複製 service_role key 貼到 .env，注意不要多複製空白")
            return
        if r.status_code >= 500:
            bad(f"Supabase 專案目前無法使用（HTTP {r.status_code}）", "專案可能被暫停或正在恢復，到 supabase.com 按 Restore project，等 1～3 分鐘")
            return
        ok(f"連得到 Supabase（HTTP {r.status_code}）")
    except Exception as e:
        bad(f"連線 Supabase 失敗：{type(e).__name__}", "檢查網路、防火牆，或換手機熱點再試")
        return

    # 5. 資料表與函式
    section("5. 資料庫結構")
    from supabase import create_client
    sb = create_client(url, key)
    schema_ok = True
    for table in ["patients", "appointments", "triage_records", "admin_users"]:
        try:
            sb.table(table).select("*").limit(1).execute()
            ok(f"資料表 {table}")
        except Exception as e:
            schema_ok = False
            text = str(getattr(e, "message", e))
            bad(f"資料表 {table} 有問題：{text[:80]}", "到 Supabase → SQL Editor，貼上並執行 sql/schema.sql 的全部內容")
    for col_check in [("patients", "patient_no, birth_date"), ("appointments", "appointment_no, status")]:
        try:
            sb.table(col_check[0]).select(col_check[1]).limit(1).execute()
        except Exception:
            schema_ok = False
            bad(f"{col_check[0]} 缺少欄位（{col_check[1]}）", "到 SQL Editor 執行 sql/002_add_sequential_ids_and_triage.sql")
    try:
        sb.table("patients").select("password_hash").limit(1).execute()
        ok("病患帳號欄位 password_hash")
    except Exception:
        schema_ok = False
        bad("patients 缺少病患帳號欄位（password_hash）", "到 SQL Editor 執行 sql/004_patient_accounts.sql")
    for table in ["clinic_questions", "clinic_knowledge"]:
        try:
            sb.table(table).select("*").limit(1).execute()
            ok(f"診所自訂資料表 {table}")
        except Exception:
            warn(f"還沒有 {table} 資料表：系統會照常使用預設題目與基礎知識庫；要使用「診所設定」頁，請到 SQL Editor 執行 sql/005_clinic_customization.sql")
    try:
        # p_max_per_slot=0 會在寫入前就回報 SLOT_FULL，不會真的新增資料，只用來確認函式存在
        sb.rpc("book_appointment_slot", {
            "p_patient_id": "00000000-0000-0000-0000-000000000000", "p_department": "脊椎外科", "p_doctor": "檢查用",
            "p_appointment_date": "2099-01-01", "p_time_slot": "早上 09:00 - 12:00", "p_max_per_slot": 0,
        }).execute()
        warn("掛號函式回應異常（沒有回報 SLOT_FULL）")
    except Exception as e:
        if "SLOT_FULL" in str(e):
            ok("掛號函式 book_appointment_slot")
        else:
            schema_ok = False
            bad("缺少掛號函式 book_appointment_slot", "到 SQL Editor 執行 sql/003_atomic_slot_capacity.sql")

    # 6. 管理員帳號
    section("6. 後台管理員帳號")
    if schema_ok:
        try:
            res = sb.table("admin_users").select("username").execute()
            if res.data:
                ok(f"已設定 {len(res.data)} 個管理員帳號")
            else:
                bad("還沒有管理員帳號（後台會登入失敗）", "執行：python set_admin_password.py admin 你的密碼")
        except Exception:
            pass

    # 7. 知識庫
    section("7. RAG 知識庫")
    db_path = os.path.join(BASE_DIR, "vector_store.db")
    pkl_path = os.path.join(BASE_DIR, "vectorizer.pkl")
    if not (os.path.exists(db_path) and os.path.exists(pkl_path)):
        bad("找不到知識庫檔案", "執行：python update.py")
    else:
        try:
            n = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM medical_chunks").fetchone()[0]
            ok(f"知識庫共 {n} 筆")
        except Exception:
            bad("知識庫檔案損壞", "執行：python update.py")

    # 8. 後端
    section("8. 後端（uvicorn）")
    try:
        r = httpx.get("http://127.0.0.1:8000/api/schedule/clinic", timeout=10)
        data = r.json()
        if data.get("status") == "success":
            ok("後端已啟動，班表查詢正常")
        else:
            bad(f"後端有啟動，但班表查詢失敗：{data.get('message', '')[:80]}", "依照上面各項的建議處理後，重新啟動後端")
    except httpx.ConnectError:
        warn("後端目前沒有啟動（要使用系統時，請另開視窗執行：uvicorn app:app --reload）")
    except ValueError:
        bad("port 8000 上的程式不是本系統的後端（回應不是 JSON）", "關掉其他佔用 8000 port 的程式，再重新執行 uvicorn app:app --reload")
    except Exception as e:
        bad(f"後端回應異常：{type(e).__name__}", "重新啟動後端，並把後端視窗的錯誤訊息提供給開發人員")

    section("9. OpenAI 聊天助理（選填）")
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        warn("沒有設定 OPENAI_API_KEY：聊天式掛號助理會使用規則版（可正常使用）。要用 GPT 請在 .env 加 OPENAI_API_KEY=...")
    else:
        try:
            import openai  # noqa: F401
            r = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                ok(f"OpenAI 金鑰可用（模型：{os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')}）")
            elif r.status_code == 401:
                bad("OpenAI 金鑰無效或已被撤銷", "到 OpenAI 後台建立新的金鑰，更新 .env 的 OPENAI_API_KEY")
            else:
                warn(f"OpenAI 回應 {r.status_code}，請確認帳號額度與網路")
        except ImportError:
            bad("沒有安裝 openai 套件", "執行 pip install -r requirements_true_rag.txt")
        except Exception as e:
            warn(f"連不到 OpenAI（{type(e).__name__}），請確認網路；連不到時會自動退回規則版")

    print("\n" + "=" * 56)
    if problems:
        print(f" 發現 {len(problems)} 個問題，請依照 👉 的建議處理後再執行一次這支程式。")
    else:
        print(" 🎉 全部檢查通過！")
    print("=" * 56)


if __name__ == "__main__":
    main()
