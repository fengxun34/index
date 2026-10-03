# 骨科智慧語音掛號助理（Orthopedic Smart Voice Triage + RAG）

依據 114 學年度第 2 學期資訊管理專題初審意見回應（B12「醫語搞定」），本團隊採「由小而大」策略，
將 POC 範圍聚焦於**社區型骨科診所的掛號情境**（一間診所、數位骨科醫師），並以 Supabase 建置病人資料與掛號紀錄資料庫，
待小診所流程驗證可行後，再逐步擴增至醫院規模與其他專科。

## 1. 功能範圍

- 小診所問診分流：AI 判斷「問題類型」（脊椎、運動傷害、關節退化、手部、足踝、骨折外傷、骨質疏鬆、兒童骨骼等）
  並推薦最擅長的診所醫師，結果以「推薦程度：高／中／低」呈現；病人畫面不會出現大醫院的次專科名稱
- 轉診建議：疑似骨骼腫瘤、需高壓氧治療等小診所無法處理的情況，會建議轉診至醫院，病人仍可選擇先讓診所醫師初步評估
- 急症（大小便失禁、骨頭外露等）提醒立即就醫；非骨科症狀（頭痛、胸悶等）提醒改掛其他科
- 全程語音操作（Web Speech API，建議使用 Chrome）：說症狀、回答問診、說出個人資料自動填入；
  掛號前可用說的選時段／換醫師（「最快的」「下週二下午」「9月30號晚上」「換林醫師」），說「確定」完成掛號；
  查詢就診紀錄時說出身分證字號與生日即可自動查詢（支援「民國七十九年一月一日」這類國字日期）
- RAG 醫療知識檢索（TF-IDF + cosine similarity，本地 SQLite 向量庫，63 筆骨科知識）：
  以「部位＋主訴＋所有問診回答」檢索，並對照規則分流結果，回報知識庫是否支持該科別，
  不會另外算出一個跟分流結果打架的答案；檢索來源與相似度預設收起，點開才顯示
- 個資保護：查詢、取消、改期、修改手機都需「身分證字號＋生日」相符，連續驗證失敗 5 次鎖定 15 分鐘；
  已有資料的身分證字號，掛號時生日也必須相符（避免冒用他人身分證字號改掉生日）
- 骨科門診掛號、掛號紀錄查詢、後台掛號總覽（病人資料與掛號紀錄存於 Supabase）
- 掛號欄位驗證（身分證字號、手機格式、科別）與同時段容額上限（避免同一時段被無限重複預約）
- 病歷號／掛號序號採連續編號，方便人工對照；AI 問診過程（部位、問答、AI 建議）會隨掛號存檔，後台可查看
- 病患帳號：用「手機號碼或身分證字號＋密碼」登入，個人資料自動帶入、就診紀錄直接顯示，
  問診時若部位與上次相同，每題都可一鍵「跟上次一樣」；忘記密碼可用身分證＋生日重設；不登入也能照常掛號
- 診所設定（`/api/admin/clinic`，需管理員登入）：依醫師問診流程**自訂各部位的問診題目與快捷選項**，
  並建立**診所專屬知識庫**（療程、處理原則、警訊），存檔後立即與基礎知識庫合併成 RAG 索引；訪談醫師可用 `docs/醫師訪談表.md`
- 今日看診清單（`/api/admin/today`，需管理員登入）：醫師看診前摘要，每位病人的初診／複診、主訴、問診問答、
  上次看診、AI 建議與 AI 從知識庫找出的提醒，可依日期、醫師篩選並列印
- 上次就診紀錄：查詢掛號時顯示最近一次已看診的科別、醫師、主訴與 AI 建議，可一鍵回診掛同一位醫師
- 修改預約資訊：改期時可改掛同科別的其他醫師；可修改聯絡手機
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

   > 📌 **已經建好資料庫、要加上病患帳號功能**：只要再執行 `sql/004_patient_accounts.sql`（新增密碼欄位，不會動到既有資料）。
   >
   > 📌 **要使用診所設定（自訂問診題目、診所知識庫）**：再執行 `sql/005_clinic_customization.sql`。沒執行也能用，只是會使用預設題目與基礎知識庫。
   >
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

**後端跑不起來、畫面一直出錯時，先執行環境檢查：**

```bash
python check_setup.py
```

會逐項檢查 Python、套件、`.env`、網路、Supabase 金鑰、資料表與掛號函式、管理員帳號、知識庫、後端是否啟動，
每一項都會顯示 ✅／❌ 與處理方式（不會修改資料庫、不會印出金鑰）。


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
  "question": "部位：腰椎。腰痛。悶痛。會延伸到腿，腳麻。一週",
  "top_k": 3,
  "metadata": { "body_part": "腰椎", "recommended_department": "脊椎外科" }
}
```
有帶 `recommended_department`（規則分流的結果）時，回傳的 `agreement` 表示知識庫是否支持分流結果：
`agree`（知識庫最相符的也是這一科）、`partial`（有相關資料，但另一科更相符，列為補充提示）、
`differ`（知識庫另外提示其他科）。`retrieved_chunks` 裡 `supports_triage: true` 的片段就是支持分流結果的依據。

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
{ "id_number": "A123456789", "birth_date": "1990/01/01" }
```
以下查詢、取消、改期、修改手機都需要 `birth_date`；查無此人與生日不符回傳同一句訊息，不透露該身分證字號是否掛過號。

回傳 `data`（所有掛號，含 `is_past` 與當次問診的 `body_part`／`main_complaint`／`ai_suggestion`）
與 `last_visit`（最近一次已看診、未取消的掛號，沒有則為 `null`）。

### 改期（可同時改掛診所其他醫師）
`POST /api/booking/reschedule`
```json
{ "id_number": "A123456789", "birth_date": "1990/01/01", "appointment_no": 12, "new_slot": "2026-08-21 下午 03:00 - 05:00", "new_doctor": "王醫師" }
```
`new_doctor` 選填，不給就維持原醫師；可改掛診所任何一位醫師。

### 病患帳號
| API | 說明 |
|---|---|
| `POST /api/account/register` | `{name, id_number, birth_date, phone, password}`，已掛過號的病人生日需相符；回傳 `token` |
| `POST /api/account/login` | `{account, password}`，`account` 可填手機或身分證字號；連續錯 5 次鎖 15 分鐘 |
| `GET /api/account/me` | Header `Authorization: Bearer <token>`，回傳個人資料、就診紀錄、上次就診、最近一次問診 |
| `POST /api/account/reset-password` | `{id_number, birth_date, new_password}`，重設後舊的登入憑證自動失效 |

登入憑證由後端簽章（預設用 service role key 衍生；可另外在 `.env` 設定 `SESSION_SECRET`），有效 7 天。

### 修改聯絡手機
`POST /api/patient/update`
```json
{ "id_number": "A123456789", "birth_date": "1990/01/01", "phone": "0987654321" }
```

### 取消掛號
`POST /api/booking/cancel`
```json
{ "id_number": "A123456789", "birth_date": "1990/01/01", "appointment_no": 12 }
```

### 診所資訊（醫師名單、擅長項目、問題類型名稱、需轉診的類型）
`GET /api/clinic/info`

### 全診所未來 7 天班表（含剩餘名額）
`GET /api/schedule/clinic?days=7`

回傳所有醫師未來 N 天每天有開的時段與剩餘名額，「直接掛號／指定醫師」與「改期」畫面都用這支 API。

### 查詢某問題類型未來 7 天班表（含剩餘名額）
`GET /api/schedule/department/{department}?days=7`

回傳該科別所有擅長醫師，未來 N 天（預設 7 天）每天有開的時段與目前剩餘名額，前端的
「自行選擇醫師／科別」與「改期」畫面都是呼叫這支 API 畫出班表格子。

### AI 自動安排最接近的門診時段
`GET /api/schedule/next-available?department=脊椎外科&doctor=高醫師`（`doctor` 選填）

依「現在時間」往後找，跳過已經開始或已額滿的時段，回傳該科別（或指定醫師）最快
可以掛上的一個時段。AI 問診分流完成、或使用者直接講出科別／醫師名稱時，前端都是
呼叫這支 API 取得建議時段，不是在前端寫死。

### 診所設定與看診前摘要（需 HTTP Basic 登入）
| 網址 | 說明 |
|---|---|
| `GET /api/admin/clinic` | 診所設定頁（問診題目、診所知識庫） |
| `GET /api/admin/today` | 今日看診清單（看診前摘要） |
| `GET /api/clinic/questions` | （公開）目前生效的問診題目，前端開啟時讀取 |
| `GET /api/admin/clinic/data`、`PUT/DELETE /api/admin/clinic/questions/{部位}` | 讀取設定、儲存／恢復某部位題目 |
| `POST /api/admin/clinic/knowledge`、`DELETE /api/admin/clinic/knowledge/{id}` | 新增／修改／刪除診所知識（存檔後自動重建 RAG 索引） |
| `GET /api/admin/today-data?date=&doctor=` | 看診前摘要資料 |

預設問診題目在 `clinic_default_questions.json`；診所改過的部位存在 `clinic_questions` 表，有自訂就用自訂。

### 後台掛號總覽（HTML 網頁，需 HTTP Basic 登入）
`GET /api/admin/all_bookings`

## 10. 診所醫師與班表怎麼調整

診所的醫師、擅長項目與每週固定看診時段都寫在 `app.py` 的 `DOCTORS`：

```python
DOCTORS = {
    "高醫師": {
        "specialty": "脊椎、骨質疏鬆",                        # 顯示給病人看的擅長項目
        "departments": ["脊椎外科", "骨質疏鬆症門診", ...],     # AI 分流到這些問題類型時會推薦這位醫師
        "weekly": {0: ["早上", "下午"], 2: ["早上", "下午"], 4: ["早上", "下午"]},
    },
    ...
}
```

- `weekly` 的 key 是星期幾（0=一…6=日），value 是當天有看診的時段代碼。
- `departments` 是 AI 分流的「內部問題類型」代碼（10 種，見 `ALLOWED_DEPARTMENTS`），**每一種都至少要有一位醫師負責**。
  給病人看的白話名稱在 `CATEGORY_LABELS`；需要建議轉診的類型在 `REFERRAL_CATEGORIES`。
- 改了醫師名單後，`index.html` 的 `CLINIC_DOCTORS`（用於辨識病人講出的醫師姓名、顯示擅長項目）也要同步修改，
  並重新執行 `python update.py`，讓知識庫裡的看診時段跟著更新。

如果某天要請假、代診或臨時加開，不用改整週的班表，在 `DOCTOR_SCHEDULE_OVERRIDES`
加一筆例外就好，會蓋掉當天原本的固定班表：

```python
DOCTOR_SCHEDULE_OVERRIDES = {
    ("高醫師", "2026-09-21"): [],                       # 高醫師當天請假，完全不看診
    ("曾醫師", "2026-09-21"): ["早上", "下午", "晚上"],  # 曾醫師當天代診，加開全天
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
