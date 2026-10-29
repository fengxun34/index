"""簡單的端對端驗證腳本：跑一次完整流程，確認系統各項功能正常。

用法：
    python smoke_test.py

會依序測試：
1. RAG 症狀查詢有回應
2. 掛號功能正常，且同一時段超過容額上限會被擋下
3. 掛號紀錄查詢得到剛剛掛的號
4. 改期功能正常，且改到醫師沒排班的時段會被擋下
5. 取消功能正常，且不是本人的掛號無法取消
5b. 上次就診紀錄欄位存在、改期可改掛診所其他醫師（不存在的醫師會被擋）、修改聯絡手機需要生日相符
5c. 查詢需要身分證＋生日；不能用別人的身分證字號＋假生日掛號來改掉對方的生日
5d. RAG 帶入分流結果時，會回報知識庫是否支持分流結果
5e. 病患帳號：註冊（生日需相符）、手機／身分證登入、登入後取得紀錄、忘記密碼後舊登入失效
5f. 診所設定：公開問診題目、管理 API 需要登入（有設定 clinic_questions 表時再測自訂題目）
5g. 聊天式掛號助理：開場、症狀問診、推薦門診時段、非骨科提醒
6. 密碼雜湊機制正確（雜湊/比對本身，不需要知道真實管理員密碼）
7. 後台登入失敗次數限制會生效（連續錯誤達上限後鎖定）
8. 後台頁面沒有登入會被拒絕（401）

測試會在 Supabase 建立一筆假病患資料（身分證字號 A199999999），
結束後會自動清除，不會留在資料庫裡；若清除失敗，腳本會提示你手動清理。

執行前請先設定好 .env（跟 app.py 用同一組 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）。
"""

import sys

from fastapi.testclient import TestClient

import app as app_module
from set_admin_password import hash_password

client = TestClient(app_module.app)

TEST_ID_NUMBER = "A199999999"
TEST_NAME = "測試病患_請忽略"
TEST_SLOT = "2099-01-05 早上 09:00 - 12:00"  # 2099-01-05 是星期一，符合高醫師的固定班表（一/三/五 早上、下午）
TEST_DOCTOR = "高醫師"
TEST_DEPARTMENT = "脊椎外科"
TEST_BIRTH = "1990/01/01"

results = []


def check(label, condition):
    ok = bool(condition)
    print(("✅ " if ok else "❌ ") + label)
    results.append(ok)
    return ok


def cleanup():
    if app_module.supabase is None:
        return
    try:
        patient = (
            app_module.supabase.table("patients")
            .select("id")
            .eq("id_number", TEST_ID_NUMBER)
            .execute()
        )
        if patient.data:
            pid = patient.data[0]["id"]
            app_module.supabase.table("appointments").delete().eq("patient_id", pid).execute()
            app_module.supabase.table("patients").delete().eq("id", pid).execute()
        print("🧹 已清除測試資料")
    except Exception as e:
        print(f"⚠️ 清除測試資料失敗，請手動到 Supabase Table Editor 刪除 id_number={TEST_ID_NUMBER} 的資料：{e}")


def main():
    if app_module.supabase is None:
        print("❌ 尚未設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，無法執行完整測試，請先設定 .env")
        sys.exit(1)

    # 清掉前一次測試可能留下的身分驗證失敗紀錄，避免被鎖住
    app_module._failed_verify_attempts.pop(TEST_ID_NUMBER, None)

    try:
        # 1. RAG 查詢
        res = client.post("/rag/answer", json={"question": "膝蓋痛合併腫脹", "top_k": 3})
        check(
            "RAG 查詢回應正常",
            res.status_code == 200 and len(res.json().get("retrieved_chunks", [])) > 0,
        )

        # 2. 掛號 + 容額上限（同一醫師同一時段連續掛號到超過上限）
        payload = {
            "name": TEST_NAME,
            "id_number": TEST_ID_NUMBER,
            "department": TEST_DEPARTMENT,
            "doctor": TEST_DOCTOR,
            "slot": TEST_SLOT,
            "birth_date": "1990/01/01",
            "phone": "0912345678",
        }
        success_count = 0
        last_status = None
        for _ in range(app_module.MAX_PATIENTS_PER_SLOT + 1):
            r = client.post("/api/booking/confirm", json=payload)
            last_status = r.json().get("status")
            if last_status == "success":
                success_count += 1
        check(
            f"容額上限生效（上限 {app_module.MAX_PATIENTS_PER_SLOT} 筆，實際成功 {success_count} 筆，"
            f"第 {app_module.MAX_PATIENTS_PER_SLOT + 1} 筆應失敗）",
            success_count == app_module.MAX_PATIENTS_PER_SLOT and last_status == "error",
        )

        # 3. 查詢掛號紀錄
        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH})
        history_data = res.json().get("data", [])
        check(
            "查詢掛號紀錄正常",
            res.status_code == 200 and len(history_data) >= 1,
        )

        # 4. 改期功能（改到 高醫師 有排班的時段應成功，改到沒排班的時段應失敗）
        reschedule_no = history_data[0]["appointment_no"] if history_data else None
        new_slot = "2099-01-02 下午 03:00 - 05:00"
        res = client.post(
            "/api/booking/reschedule",
            json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "appointment_no": reschedule_no, "new_slot": new_slot},
        )
        reschedule_ok = res.status_code == 200 and res.json().get("status") == "success"
        check("改期到醫師有排班的時段應成功", reschedule_ok)

        res = client.post(
            "/api/booking/reschedule",
            json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "appointment_no": reschedule_no, "new_slot": "2099-01-02 晚上 06:00 - 09:00"},
        )
        check(
            "改期到醫師沒排班的時段應被擋下",
            res.status_code == 200 and res.json().get("status") == "error",
        )

        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH})
        history_data = res.json().get("data", [])
        rescheduled = next((h for h in history_data if h["appointment_no"] == reschedule_no), None)
        check(
            "改期後查詢紀錄看得到新時段",
            rescheduled is not None and new_slot in rescheduled.get("slot", ""),
        )

        # 5. 取消功能（本人可以取消；冒用別人身分證字號取消應被擋下）
        cancel_no = history_data[-1]["appointment_no"] if history_data else None
        res = client.post(
            "/api/booking/cancel",
            json={"id_number": "A987654321", "birth_date": TEST_BIRTH, "appointment_no": cancel_no},
        )
        check(
            "用錯誤的身分證字號取消別人的掛號應被擋下",
            res.status_code == 200 and res.json().get("status") == "error",
        )

        res = client.post(
            "/api/booking/cancel",
            json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "appointment_no": cancel_no},
        )
        check(
            "本人取消掛號應成功",
            res.status_code == 200 and res.json().get("status") == "success",
        )

        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH})
        cancelled = next((h for h in res.json().get("data", []) if h["appointment_no"] == cancel_no), None)
        check(
            "取消後查詢紀錄狀態應變成 cancelled",
            cancelled is not None and cancelled.get("status") == "cancelled",
        )

        # 5b. 上次就診紀錄／修改預約資訊
        check("查詢紀錄會回傳 last_visit 欄位（上次就診紀錄）", "last_visit" in res.json())
        res = client.post(
            "/api/booking/reschedule",
            json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "appointment_no": reschedule_no, "new_slot": "2099-01-05 早上 09:00 - 12:00", "new_doctor": "張醫師"},
        )
        check("改期時不能改掛不存在的醫師", res.status_code == 200 and res.json().get("status") == "error")
        res = client.post(
            "/api/booking/reschedule",
            json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "appointment_no": reschedule_no, "new_slot": "2099-01-05 晚上 06:00 - 09:00", "new_doctor": "林醫師"},
        )
        check("改期時可以改掛診所其他醫師（林醫師週一晚上）", res.status_code == 200 and res.json().get("status") == "success")
        res = client.post("/api/patient/update", json={"id_number": TEST_ID_NUMBER, "birth_date": "1990/01/02", "phone": "0987654321"})
        check("生日不符時不能修改聯絡手機", res.status_code == 200 and res.json().get("status") == "error")
        res = client.post("/api/patient/update", json={"id_number": TEST_ID_NUMBER, "birth_date": "1990/01/01", "phone": "0987654321"})
        check("生日相符時可以修改聯絡手機", res.status_code == 200 and res.json().get("status") == "success")

        # 5c. 身分驗證：生日不符查不到；用同一個身分證字號＋不同生日掛號應被擋下
        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER, "birth_date": "1991/02/03"})
        check("生日不符時查不到掛號紀錄", res.status_code == 200 and res.json().get("status") == "error")
        res = client.post("/api/booking/confirm", json={**payload, "birth_date": "1991/02/03", "slot": "2099-01-07 早上 09:00 - 12:00"})
        check("不能用別人的身分證字號＋不同生日掛號", res.status_code == 200 and res.json().get("status") == "error")
        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER, "birth_date": "1990-1-1"})
        check("生日換個寫法（1990-1-1）也能查詢", res.status_code == 200 and res.json().get("status") == "success")
        app_module._failed_verify_attempts.pop(TEST_ID_NUMBER, None)

        # 5d. RAG 對照分流結果
        res = client.post("/rag/answer", json={"question": "部位：腰椎。腰痛。延伸到腿，腳麻", "top_k": 3, "metadata": {"recommended_department": "脊椎外科"}})
        check("RAG 會回報知識庫是否支持分流結果", res.status_code == 200 and res.json().get("agreement") in ("agree", "partial", "differ"))

        # 5e. 病患帳號
        acct_phone = "0900999001"
        res = client.post("/api/account/register", json={"name": TEST_NAME, "id_number": TEST_ID_NUMBER, "birth_date": "1991/02/03", "phone": acct_phone, "password": "smoke123"})
        check("帳號：生日不符不能註冊", res.status_code == 200 and res.json().get("status") == "error")
        res = client.post("/api/account/register", json={"name": TEST_NAME, "id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "phone": acct_phone, "password": "smoke123"})
        token = res.json().get("token")
        check("帳號：既有病人註冊成功", res.status_code == 200 and res.json().get("status") == "success" and token)
        app_module._failed_login_attempts.pop(f"patient:{acct_phone}", None)
        res = client.post("/api/account/login", json={"account": acct_phone, "password": "wrong-pass"})
        check("帳號：密碼錯誤不能登入", res.json().get("status") == "error")
        res = client.post("/api/account/login", json={"account": acct_phone, "password": "smoke123"})
        check("帳號：用手機登入", res.json().get("status") == "success")
        res = client.post("/api/account/login", json={"account": TEST_ID_NUMBER, "password": "smoke123"})
        check("帳號：用身分證登入", res.json().get("status") == "success")
        res = client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"})
        check("帳號：登入後取得個人資料與就診紀錄", res.status_code == 200 and res.json().get("profile", {}).get("id_number") == TEST_ID_NUMBER and len(res.json().get("data", [])) >= 1)
        res = client.post("/api/account/reset-password", json={"id_number": TEST_ID_NUMBER, "birth_date": TEST_BIRTH, "new_password": "smoke456"})
        check("帳號：忘記密碼可重設", res.json().get("status") == "success")
        res = client.get("/api/account/me", headers={"Authorization": f"Bearer {token}"})
        check("帳號：重設密碼後舊的登入失效", res.status_code == 401)
        app_module._failed_login_attempts.pop(f"patient:{acct_phone}", None)

        # 5f. 診所設定
        res = client.get("/api/clinic/questions")
        check("診所設定：可取得問診題目", res.status_code == 200 and len(res.json().get("questions", {})) >= 10)
        check("診所設定：管理 API 需要登入", client.get("/api/admin/clinic/data").status_code == 401)
        check("今日看診清單：需要登入", client.get("/api/admin/today-data").status_code == 401)

        # 5g. 聊天式掛號助理
        res = client.post("/api/agent/chat", json={"message": "", "state": None}).json()
        check("聊天助理：開場有回覆與快捷選項", res.get("reply") and res.get("quick_replies"))
        res = client.post("/api/agent/chat", json={"message": "我膝蓋痛", "state": res["state"]}).json()
        check("聊天助理：說出症狀後開始問診", res["state"].get("step") == "qa" and res["state"].get("part") == "膝關節")
        st = res["state"]
        for _ in range(15):
            if st.get("step") != "qa":
                break
            res = client.post("/api/agent/chat", json={"message": "跳過這題", "state": st}).json()
            st = res["state"]
        check("聊天助理：問診完推薦醫師與時段", st.get("step") == "propose" and st.get("slot", {}).get("doctor") in app_module.DOCTORS)
        res = client.post("/api/agent/chat", json={"message": "我胸悶", "state": None}).json()
        check("聊天助理：非骨科症狀會提醒改掛其他科", "心臟內科" in res.get("reply", ""))

        # 6. 密碼雜湊機制本身正確（不需要知道真實管理員密碼，直接測雜湊/比對函式）
        test_password = "測試密碼_請忽略_Xk9!2p"
        hashed = hash_password(test_password)
        check(
            "雜湊格式正確且能用正確密碼驗證通過",
            hashed.startswith("pbkdf2_sha256$") and app_module.verify_password(test_password, hashed),
        )
        check("錯誤密碼應驗證失敗", not app_module.verify_password("錯誤密碼", hashed))
        check(
            "舊版明文密碼格式應安全地驗證失敗（不會噴例外）",
            not app_module.verify_password(test_password, test_password),
        )

        # 7. 後台登入失敗次數限制（連續錯誤達上限後應鎖定，回傳 429）
        # 這裡刻意用純英數字帳號：HTTP Basic Auth 的帳密規範上要編碼成 ASCII/Latin-1，
        # 若帳號含中文字，不同版本的 httpx 在編碼 Authorization 標頭時行為可能不一致，
        # 可能導致請求在還沒進到 verify_admin 前就被 FastAPI 的 HTTPBasic 攔截、
        # 根本沒有真正執行到失敗次數計算，讓這項測試失去意義。
        lockout_username = "smoke_test_nonexistent_user"
        app_module._failed_login_attempts.pop(lockout_username, None)
        last_status = None
        for _ in range(app_module.MAX_LOGIN_ATTEMPTS):
            r = client.get("/api/admin/all_bookings", auth=(lockout_username, "wrong"))
            last_status = r.status_code
        check(f"連續錯誤 {app_module.MAX_LOGIN_ATTEMPTS} 次前應維持 401", last_status == 401)
        r = client.get("/api/admin/all_bookings", auth=(lockout_username, "wrong"))
        check("達到失敗次數上限後應被鎖定（429）", r.status_code == 429)
        app_module._failed_login_attempts.pop(lockout_username, None)

        # 8. 後台頁面未登入應被拒絕
        res = client.get("/api/admin/all_bookings")
        check("後台未登入應被拒絕（401）", res.status_code == 401)

    finally:
        cleanup()

    print()
    if all(results):
        print("🎉 全部測試通過！")
        sys.exit(0)
    else:
        print("⚠️ 有測試沒通過，請往上檢查紅色 ❌ 項目")
        sys.exit(1)


if __name__ == "__main__":
    main()
