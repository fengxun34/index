"""簡單的端對端驗證腳本：跑一次完整流程，確認系統各項功能正常。

用法：
    python smoke_test.py

會依序測試：
1. RAG 症狀查詢有回應
2. 掛號功能正常，且同一時段超過容額上限會被擋下
3. 掛號紀錄查詢得到剛剛掛的號
4. 後台頁面沒有登入會被拒絕（401）

測試會在 Supabase 建立一筆假病患資料（身分證字號 A199999999），
結束後會自動清除，不會留在資料庫裡；若清除失敗，腳本會提示你手動清理。

執行前請先設定好 .env（跟 app.py 用同一組 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）。
"""

import sys

from fastapi.testclient import TestClient

import app as app_module

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
        check(
            "查詢掛號紀錄正常",
            res.status_code == 200 and len(res.json().get("data", [])) >= 1,
        )

        # 4. 後台頁面未登入應被拒絕
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
