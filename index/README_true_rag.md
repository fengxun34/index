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
- 上次就診紀錄：查詢掛號時顯示最近一次已看診的科別、醫師、主訴與 AI 建議，可一鍵回診掛同一位醫師
- 修改預約資訊：改期時可改掛同科別的其他醫師；可修改聯絡手機（需身分證字號＋生日相符）
- X 光辨識器（示範版，**目前停用**，首頁不顯示；要開放時把 `index.html` 的 `ENABLE_XRAY` 改成 `true`）：上傳／拍攝 X 光片，檢查是否為灰階 X 光影像、提供亮度／對比／放大／反相檢視，
  並依拍攝部位建議次專科、直接帶去掛號。**不會自動判讀骨折或病灶**，圖片只在使用者裝置上處理、不上傳
- 復健／用藥提醒：存在使用者裝置（localStorage），網頁開著時定時跳出提醒＋語音播報，
  可開啟瀏覽器通知，或匯出 `.ics` 匯入手機行事曆（關掉網頁也會通知）
- 復健影片教學：各部位常見居家復健動作（步驟、次數、注意事項、語音唸步驟），可一鍵加入每日復健提醒

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

回傳 `data`（所有掛號，含 `is_past` 與當次問診的 `body_part`／`main_complaint`／`ai_suggestion`）
與 `last_visit`（最近一次已看診、未取消的掛號，沒有則為 `null`）。

### 改期（可同時改掛同科別其他醫師）
`POST /api/booking/reschedule`
```json
{ "id_number": "A123456789", "appointment_no": 12, "new_slot": "2026-08-21 下午 03:00 - 05:00", "new_doctor": "王醫師" }
```
`new_doctor` 選填，不給就維持原醫師；新醫師必須擅長原掛號的科別。

### 修改聯絡手機
`POST /api/patient/update`
```json
{ "id_number": "A123456789", "birth_date": "1990/01/01", "phone": "0987654321" }
```
生日需與掛號時填寫的相符才能修改。

### 查詢某科別未來 7 天班表（含剩餘名額）
`GET /api/schedule/department/{department}?days=7`

回傳該科別所有擅長醫師，未來 N 天（預設 7 天）每天有開的時段與目前剩餘名額，前端的
「自行選擇醫師／科別」與「改期」畫面都是呼叫這支 API 畫出班表格子。

### AI 自動安排最接近的門診時段
`GET /api/schedule/next-available?department=脊椎外科&doctor=高醫師`（`doctor` 選填）

依「現在時間」往後找，跳過已經開始或已額滿的時段，回傳該科別（或指定醫師）最快
可以掛上的一個時段。AI 問診分流完成、或使用者直接講出科別／醫師名稱時，前端都是
呼叫這支 API 取得建議時段，不是在前端寫死。

### 後台掛號總覽（HTML 網頁，需 HTTP Basic 登入）
`GET /api/admin/all_bookings`

## 10. 醫師班表資料怎麼調整

醫師的擅長科別與每週固定看診時段都寫在 `app.py` 的 `DOCTORS`：

```python
DOCTORS = {
    "高醫師": {"departments": ["脊椎外科"], "weekly": {0: ["早上", "下午"], 2: ["早上", "下午"], 4: ["早上", "下午"]}},
    ...
}
```

`weekly` 的 key 是星期幾（0=一…6=日），value 是當天有看診的時段代碼。一位醫師可以有
多個擅長科別（例如 `"王醫師": {"departments": ["足踝外科", "運動醫學科"], ...}`）。

如果某天要請假、代診或臨時加開，不用改整週的班表，在 `DOCTOR_SCHEDULE_OVERRIDES`
加一筆例外就好，會蓋掉當天原本的固定班表：

```python
DOCTOR_SCHEDULE_OVERRIDES = {
    ("高醫師", "2026-09-21"): [],                       # 高醫師當天請假，完全不看診
    ("謝醫師", "2026-09-21"): ["早上", "下午", "晚上"],  # 謝醫師當天代診，加開全天
}
```

改完 `DOCTORS` 或 `DOCTOR_SCHEDULE_OVERRIDES` 後，重新啟動 `uvicorn app:app --reload`
就會套用新班表（`--reload` 模式下存檔就會自動套用，不用重啟）。

## 11. 復健影片怎麼換成院方自己的影片

復健動作資料寫在 `index.html` 的 `REHAB_EXERCISES`。每個動作的 `videoUrl` 預設是空的，
畫面會顯示「看示範影片」按鈕（連到 YouTube 搜尋結果）。把院方拍攝或指定的 YouTube 網址貼進
`videoUrl`，畫面就會直接嵌入播放：

```js
{ id: "slr", part: "膝關節", name: "直膝抬腿", ..., videoUrl: "https://www.youtube.com/watch?v=影片ID" }
```

## 12. 架構

- Frontend: 骨科問診 UI（React, CDN 版）+ API 呼叫
- RAG Embedding: `TfidfVectorizer`（本地 SQLite 向量庫，僅供醫療知識檢索，非病人個資）
- 病人資料 / 掛號紀錄: Supabase（PostgreSQL）
- Retrieval: cosine similarity
- Generation: API 端整合 retrieved chunks 後輸出建議
