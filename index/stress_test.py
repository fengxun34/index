"""壓力測試腳本：驗證系統在高並發下是否還能正確運作。

用法：
    python stress_test.py

會測試兩個情境：
1. 高並發搶同一個時段掛號 —— 用多個 thread「同時」對同一位醫師的同一個
   時段送出遠多於容額上限的掛號請求，驗證容額上限（同一醫師同一時段最多
   MAX_PATIENTS_PER_SLOT 人）在併發湧入時仍然精準生效，不會超賣。
   （這個情境原本會失敗——本地檢查再寫入不是同一筆交易，高並發下可能一起
   通過檢查、一起塞進去；已改用資料庫端的 advisory lock 原子操作修正，
   詳見 sql/003_atomic_slot_capacity.sql。）
2. 一般吞吐量 —— 對 RAG 查詢端點送出大量並發請求，統計成功率與回應時間
   （平均、95 百分位、最慢），評估系統在流量尖峰時的穩定度。

測試會在 Supabase 建立假病患資料，結束後自動清除；若清除失敗，腳本會提示
你手動清理（身分證字號開頭都是 A29）。

執行前請先設定好 .env（跟 app.py 用同一組 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）。
"""

import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)

TEST_ID_PREFIX = "A29"
TEST_DOCTOR = "高醫師"
TEST_DEPARTMENT = "脊椎外科"
TEST_SLOT = "2099-02-01 早上 09:00 - 12:00"

CONCURRENT_BOOKING_REQUESTS = 20  # 遠大於容額上限，刻意製造搶位情境
THROUGHPUT_REQUESTS = 50
THROUGHPUT_WORKERS = 20


def cleanup(id_numbers):
    if app_module.supabase is None:
        return
    cleaned = 0
    for id_number in id_numbers:
        try:
            patient = (
                app_module.supabase.table("patients")
                .select("id")
                .eq("id_number", id_number)
                .execute()
            )
            if patient.data:
                pid = patient.data[0]["id"]
                app_module.supabase.table("appointments").delete().eq("patient_id", pid).execute()
                app_module.supabase.table("patients").delete().eq("id", pid).execute()
                cleaned += 1
        except Exception as e:
            print(f"⚠️ 清除 {id_number} 失敗，請手動到 Supabase 清理：{e}")
    print(f"🧹 已清除 {cleaned} 筆測試資料")


def book_one(idx):
    id_number = f"{TEST_ID_PREFIX}{idx:07d}"
    payload = {
        "name": f"壓測病患{idx}",
        "id_number": id_number,
        "department": TEST_DEPARTMENT,
        "doctor": TEST_DOCTOR,
        "slot": TEST_SLOT,
        "birth_date": "1990/01/01",
        "phone": "0912345678",
    }
    t0 = time.perf_counter()
    res = client.post("/api/booking/confirm", json=payload)
    elapsed = time.perf_counter() - t0
    return id_number, res.json().get("status"), elapsed


def test_slot_capacity_under_concurrency():
    limit = app_module.MAX_PATIENTS_PER_SLOT
    print(f"\n【情境一】{CONCURRENT_BOOKING_REQUESTS} 人同時搶 {TEST_DOCTOR} 的 {TEST_SLOT}"
          f"（容額上限 {limit} 人）")

    id_numbers = [f"{TEST_ID_PREFIX}{i:07d}" for i in range(CONCURRENT_BOOKING_REQUESTS)]
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENT_BOOKING_REQUESTS) as pool:
        futures = [pool.submit(book_one, i) for i in range(CONCURRENT_BOOKING_REQUESTS)]
        for f in as_completed(futures):
            results.append(f.result())

    success_count = sum(1 for _, status, _ in results if status == "success")
    error_count = sum(1 for _, status, _ in results if status == "error")
    print(f"  併發送出 {CONCURRENT_BOOKING_REQUESTS} 筆，成功 {success_count} 筆、被擋下 {error_count} 筆")

    # 不只看 API 回應，直接查資料庫確認真正寫進去的 confirmed 筆數，
    # 避免「API 說成功但其實資料庫被寫超過」這種更嚴重的併發問題被漏掉。
    slot_date, slot_time = TEST_SLOT.split(" ", 1)
    actual = (
        app_module.supabase.table("appointments")
        .select("id", count="exact")
        .eq("doctor", TEST_DOCTOR)
        .eq("appointment_date", slot_date)
        .eq("time_slot", slot_time)
        .eq("status", "confirmed")
        .execute()
    )
    actual_count = actual.count or 0
    ok = success_count == limit and actual_count == limit
    print(("✅ " if ok else "❌ ") +
          f"容額上限在高併發下仍然精準（成功數={success_count}，資料庫實際筆數={actual_count}，上限={limit}）")

    cleanup(id_numbers)
    return ok


def test_throughput():
    print(f"\n【情境二】RAG 查詢端點併發吞吐量測試（{THROUGHPUT_REQUESTS} 個請求、{THROUGHPUT_WORKERS} 併發）")

    questions = ["膝蓋痛合併腫脹", "腰痛延伸到腿部", "肩膀舉不起來", "手腕麻木無力", "腳踝扭傷"]

    def query_one(idx):
        t0 = time.perf_counter()
        res = client.post("/rag/answer", json={"question": questions[idx % len(questions)], "top_k": 3})
        return res.status_code == 200, time.perf_counter() - t0

    results = []
    with ThreadPoolExecutor(max_workers=THROUGHPUT_WORKERS) as pool:
        futures = [pool.submit(query_one, i) for i in range(THROUGHPUT_REQUESTS)]
        for f in as_completed(futures):
            results.append(f.result())

    success_count = sum(1 for ok, _ in results if ok)
    times = sorted(t for _, t in results)
    avg_time = statistics.mean(times)
    p95_time = times[max(int(len(times) * 0.95) - 1, 0)]
    max_time = times[-1]

    print(f"  成功率：{success_count}/{THROUGHPUT_REQUESTS}（{success_count / THROUGHPUT_REQUESTS:.0%}）")
    print(f"  回應時間：平均 {avg_time * 1000:.0f}ms，95 百分位 {p95_time * 1000:.0f}ms，最慢 {max_time * 1000:.0f}ms")

    ok = success_count == THROUGHPUT_REQUESTS
    print(("✅ " if ok else "❌ ") + "所有併發請求都成功回應")
    return ok


def main():
    if app_module.supabase is None:
        print("❌ 尚未設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，無法執行完整測試，請先設定 .env")
        sys.exit(1)

    results = [
        test_slot_capacity_under_concurrency(),
        test_throughput(),
    ]

    print()
    if all(results):
        print("🎉 壓力測試全部通過！")
        sys.exit(0)
    else:
        print("⚠️ 有測試沒通過，請往上檢查紅色 ❌ 項目")
        sys.exit(1)


if __name__ == "__main__":
    main()
