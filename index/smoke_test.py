"""簡單的端對端驗證腳本：跑一次完整流程，確認系統各項功能正常。

用法：
    python smoke_test.py

會依序測試：
1. RAG 症狀查詢有回應
2. 掛號功能正常，且同一時段超過容額上限會被擋下
3. 掛號紀錄查詢得到剛剛掛的號
4. 改期功能正常，且改到醫師沒排班的時段會被擋下
5. 取消功能正常，且不是本人的掛號無法取消
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
TEST_SLOT = "2099-01-01 早上 09:00 - 12:00"
TEST_DOCTOR = "高醫師"
TEST_DEPARTMENT = "脊椎外科"

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
        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER})
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
            json={"id_number": TEST_ID_NUMBER, "appointment_no": reschedule_no, "new_slot": new_slot},
        )
        reschedule_ok = res.status_code == 200 and res.json().get("status") == "success"
        check("改期到醫師有排班的時段應成功", reschedule_ok)

        res = client.post(
            "/api/booking/reschedule",
            json={"id_number": TEST_ID_NUMBER, "appointment_no": reschedule_no, "new_slot": "2099-01-02 晚上 06:00 - 09:00"},
        )
        check(
            "改期到醫師沒排班的時段應被擋下",
            res.status_code == 200 and res.json().get("status") == "error",
        )

        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER})
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
            json={"id_number": "A987654321", "appointment_no": cancel_no},
        )
        check(
            "用錯誤的身分證字號取消別人的掛號應被擋下",
            res.status_code == 200 and res.json().get("status") == "error",
        )

        res = client.post(
            "/api/booking/cancel",
            json={"id_number": TEST_ID_NUMBER, "appointment_no": cancel_no},
        )
        check(
            "本人取消掛號應成功",
            res.status_code == 200 and res.json().get("status") == "success",
        )

        res = client.post("/api/booking/history", json={"id_number": TEST_ID_NUMBER})
        cancelled = next((h for h in res.json().get("data", []) if h["appointment_no"] == cancel_no), None)
        check(
            "取消後查詢紀錄狀態應變成 cancelled",
            cancelled is not None and cancelled.get("status") == "cancelled",
        )

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
        lockout_username = "smoke_test_不存在的帳號"
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
