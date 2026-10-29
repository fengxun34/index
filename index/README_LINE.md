# LINE 掛號與回診提醒設定（Windows）

## 本次功能

- 登入病患端後，以 10 分鐘有效、一次性綁定碼綁定診所 LINE。
- 完成掛號、改期、取消時，資料庫自動建立通知，排程每分鐘發送。
- 台灣時間每日 09:00 起，發送明日已預約門診提醒；沒有預約就不會通知。
- 可在病患端解除綁定，或在 LINE 傳「停止提醒」「開啟提醒」。
- 訊息僅含門診日期、時段、醫師，不帶身分證、生日、問診回答。
- 這是已預約門診提醒，不會自行判斷病患何時應回診；醫師指定回診日需另建立資料與流程。

## 1. 安裝檔案

先備份目前 app.py、index.html。把本次 app.py、index.html、line_bot.py、line_worker.py
放在 C:\Users\USER\Desktop\0930。sql 資料夾供 SQL Editor 使用。
其他原有檔案、知識庫、.env 保留。

終端機執行：

```bat
cd /d C:\Users\USER\Desktop\0930
python -m pip install tzdata
```

tzdata 提供 Windows 的 Asia/Taipei 時區資料。其他套件沿用原專案已安裝的 FastAPI、httpx、supabase、python-dotenv。

## 2. 建立資料表

Supabase 選同一個專案 → SQL Editor → New query。
貼上 sql/006_line_notifications.sql 全文並按 Run。
需要既有 patients、appointments 表；不刪除原資料。

它新增 line_links、line_link_codes、line_outbox，以及掛號變更通知 trigger。
尚未绑定的病患不建立掛號通知，不會追溯發送綁定前的掛號成功訊息。

## 3. 建立 LINE 官方帳號

1. 開啟 https://manager.line.biz/，建立供診所使用的 LINE 官方帳號。
2. 在官方帳號設定啟用 Messaging API，選擇或建立 Provider。
3. 開啟 https://developers.line.biz/console/，選同一個 Provider 下的 Messaging API channel。
4. Basic settings 找 Channel secret；Messaging API 分頁取得 Channel access token（long-lived，Issue）。
5. 在 Messaging API 分頁找到加好友 QR code 或官方帳號加好友連結，供病患使用。

這些是 LINE 的金鑰，與 Supabase 金鑰不同。不要傳送給別人。

在原本 .env 後面加：

```dotenv
LINE_CHANNEL_SECRET=你的Channel_secret
LINE_CHANNEL_ACCESS_TOKEN=你的Channel_access_token
LINE_FRIEND_URL=https://line.me/R/ti/p/你的官方帳號ID
```

LINE_FRIEND_URL 請填官方帳號實際提供的加好友網址，不要保留示意文字。
保存後重新啟動後端：

```bat
python -m uvicorn app:app --reload
```

## 4. 給 LINE 一個公開 HTTPS Webhook

LINE 無法連到你電腦的 127.0.0.1。展示時可使用 ngrok；正式運作應部署到持續開機的 HTTPS 伺服器。

1. 到 https://ngrok.com/download/windows 安裝 ngrok，建立帳號。
2. 從 ngrok 帳號頁取得 authtoken，在新終端機執行（不用貼给別人）：

```bat
ngrok config add-authtoken 你的ngrok_authtoken
ngrok http 8000
```

3. ngrok 顯示一個 https://... 公開網址。這會讓外部請求進入你的後端，請僅以測試資料展示。
4. LINE Developers → Messaging API → Webhook settings → Webhook URL 填：

```text
https://你的公開網址/api/line/webhook
```

5. 按 Verify，成功後開啟 Use webhook。
6. 若機器人同時回覆官方預設訊息，可在 Official Account Manager 關閉自動回應訊息。

若公開網址更換，要同步修改 Webhook URL。後端與 ngrok 都要保持開啟。

## 5. 啟動排程

另外開啟一個 cmd 視窗：

```bat
cd /d C:\Users\USER\Desktop\0930
python line_worker.py
```

此視窗每分鐘取通知並發送。需保持開啟；關閉後不會準時發送。
後端、ngrok、worker 是三個獨立視窗。排程資料在 Supabase，不靠瀏覽器保持開啟。

## 6. 綁定與驗證

1. 病患端重新整理、登入，首頁上方選「綁定 LINE 通知」。
2. 複製彈窗裡的「綁定 一串代碼」。代碼等同一次性授權，僅傳給自己的診所官方帳號。
3. 加診所 LINE 好友，在一對一聊天傳這段文字，應收到「綁定成功」。
4. 新增一次測試掛號，1 分鐘左右收到掛號成功通知。
5. 改期、取消各測試一次，確認通知內容。
6. 明日預約在台灣時間 09:00 後，由排程自動通知；同一日期／時段／醫師不重複建立提醒。
7. 傳「停止提醒」後新通知不再發送；解除綁定後需重新取得代碼才能再次綁定。

可於 Supabase Table Editor 查看 line_outbox：pending 待送、processing 處理中、sent LINE API 已接受、skipped 已略過、failed 超過重試上限。
sent 不代表病患已讀。封鎖官方帳號或 LINE 配額問題可能影響接收。
失敗每 5 分鐘重試，最多 10 次；同一通知用相同 retry key 防止重複推播。

## 測試與限制

本次 Python 語法與 6 項離線測試已驗證：簽章篡改、取消／改期略過、停止通知、成功送出狀態、失敗重試。
Supabase migration、LINE 真實綁定／收訊需在你的環境執行驗證，尚未實測。
LINE 推播受官方帳號方案與配額限制，實際費用以帳號後台為準。

離線測試：

```bat
python -m unittest test_line_bot.py
```

官方參考：
- https://developers.line.biz/en/docs/messaging-api/getting-started/
- https://developers.line.biz/en/docs/messaging-api/building-bot/
- https://developers.line.biz/en/docs/messaging-api/receiving-messages/
- https://developers.line.biz/en/docs/messaging-api/retrying-api-request/
- https://ngrok.com/download/windows
