# 骨科智慧語音掛號助理（Orthopedic Smart Voice Triage + RAG）

依據 114 學年度第 2 學期資訊管理專題初審意見回應（B12「醫語搞定」），本團隊採「由小而大」策略，
將 POC 範圍聚焦於**骨科掛號情境**，並以 Supabase 建置病人資料與掛號紀錄資料庫，
待骨科流程驗證可行後，再逐步擴增至其他專科。

## 1. 功能範圍

- 骨科次專科智慧問診分流：脊椎外科、運動醫學科、關節重建科、手外科、足踝外科、骨折創傷科、骨質疏鬆症門診
- 語音／文字雙模式問診（Web Speech API）
- RAG 醫療知識檢索（TF-IDF + cosine similarity，本地 SQLite 向量庫）
- 骨科門診掛號、掛號紀錄查詢、後台掛號總覽（病人資料與掛號紀錄存於 Supabase）
- 掛號欄位驗證（身分證字號、手機格式、科別）與同時段容額上限（避免同一時段被無限重複預約）
- 病歷號／掛號序號採連續編號，方便人工對照；AI 問診過程（部位、問答、AI 建議）會隨掛號存檔，後台可查看

## 2. 安裝

```bash
pip install -r requirements_true_rag.txt
```

## 3. 設定 Supabase（病人資料 + 掛號紀錄）

1. 於 [Supabase](https://supabase.com) 建立新專案。
2. 開啟專案的 **SQL Editor**，貼上並執行 `sql/schema.sql`，建立 `patients`（病人資料）、
   `appointments`（掛號紀錄）、`admin_users`（後台管理員登入）與 `triage_records`（AI 問診紀錄）四張資料表。

   > 📌 如果你**已經**執行過舊版的 `sql/schema.sql`（`patients`／`appointments` 已經存在），
   > 改執行 `sql/002_add_sequential_ids_and_triage.sql` 來補上病歷號、掛號序號欄位與
   > `triage_records` 表，不會動到既有資料。
3. 到 **Project Settings → API**，複製 `Project URL` 與 `service_role` key。
4. 複製 `.env.example` 為 `.env`，填入：

   ```bash
   SUPABASE_URL=https://your-project-ref.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
   ```

   > ⚠️ `service_role` key 具完整資料庫存取權限，僅供後端使用，切勿放入前端或提交到版本控制
   > （`.env` 已加入 `.gitignore`）。後端以 Service Role 存取資料庫，因此 `sql/schema.sql`
   > 對 `patients`／`appointments`／`admin_users` 開啟 Row Level Security 且未額外開放公開政策，
   > 病人個資只能透過後端 API 存取。

## 4. 設定後台管理員帳號

後台掛號總覽頁面（`/api/admin/all_bookings`）會顯示所有病患姓名與身分證字號，因此需要登入才能查看。
帳號密碼存在 `admin_users` 表裡，設定／修改都不用手寫 SQL，
直接執行（確認 `.env` 已設定好 `SUPABASE_URL`／`SUPABASE_SERVICE_ROLE_KEY`）：

```bash
python set_admin_password.py admin 你的強密碼
```

之後打開後台頁面時，瀏覽器會跳出帳號密碼輸入框，輸入上面設定的帳密才能查看。
之後要更改密碼，重新執行同一個指令（換新密碼）即可覆蓋。

## 5. 建立 RAG 知識庫（本地，與病人資料無關）

```bash
python update.py
```

會讀取 `medical_sheet.csv`（骨科次專科症狀知識庫），產生 `vector_store.db` 與 `vectorizer.pkl`。

## 6. 啟動後端 API

```bash
uvicorn app:app --reload
```

啟動後預設位址：`http://127.0.0.1:8000`

## 7. 前端

- `index.html` + `index.css`
- 直接用瀏覽器開啟 `index.html` 即可（需搭配步驟 6 的後端服務）

## 8. 驗證系統是否正常（可選）

改完程式碼、或展示前想確認一次系統沒壞掉，可以跑：

```bash
python smoke_test.py
```

會依序測試 RAG 查詢、掛號（含容額上限）、掛號紀錄查詢、後台登入保護，測試會建立一筆假資料
（身分證字號 `A199999999`）並在結束後自動清除，不會留在資料庫裡。

## 9. API

### 問診 RAG 建議
`POST /rag/answer`
```json
{
  "question": "主訴部位：腰椎；主訴描述：腰痛 合併 腳麻；系統建議次專科：脊椎外科。請給掛號建議",
  "top_k": 3,
  "metadata": { "body_part": "腰椎", "recommended_department": "脊椎外科" }
}
```

### 確認掛號（寫入 Supabase：patients + appointments）
`POST /api/booking/confirm`
```json
{
  "name": "王小明",
  "id_number": "A123456789",
  "department": "脊椎外科",
  "doctor": "高醫師",
  "slot": "2026-08-20 早上 09:00 - 12:00",
  "birth_date": "1990/01/01",
  "phone": "0912345678"
}
```

### 查詢個人掛號紀錄
`POST /api/booking/history`
```json
{ "id_number": "A123456789" }
```

### 後台掛號總覽（HTML 網頁，需 HTTP Basic 登入）
`GET /api/admin/all_bookings`

## 10. 架構

- Frontend: 骨科問診 UI（React, CDN 版）+ API 呼叫
- RAG Embedding: `TfidfVectorizer`（本地 SQLite 向量庫，僅供醫療知識檢索，非病人個資）
- 病人資料 / 掛號紀錄: Supabase（PostgreSQL）
- Retrieval: cosine similarity
- Generation: API 端整合 retrieved chunks 後輸出建議
