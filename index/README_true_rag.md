# True RAG Demo for Smart Voice Triage

## 1. 安裝
```bash
pip install -r requirements_true_rag.txt
```

## 2. 啟動後端 API
```bash
uvicorn app:app --reload
```
啟動後預設位址：`http://127.0.0.1:8000`

## 3. 前端檔案
- `index_true_rag.html`
- `index_true_rag.css`

將 HTML 與 CSS 放在同一個資料夾，直接用瀏覽器開啟 `index_true_rag.html` 即可。

## 4. 架構
- Frontend: 問診 UI + API 呼叫
- Embedding: `TfidfVectorizer`
- Vector DB: SQLite + JSON vectors
- Retrieval: cosine similarity
- Generation: API 端整合 retrieved chunks 後輸出答案

## 5. API
### Health
`GET /health`

### Search
`POST /rag/search`
```json
{
  "query": "胸悶 呼吸不順",
  "top_k": 4
}
```

### Answer
`POST /rag/answer`
```json
{
  "question": "主訴部位：胸；主訴描述：胸悶 呼吸不順；系統建議科別：心臟內科。請給掛號建議",
  "top_k": 4,
  "metadata": {
    "body_part": "胸",
    "recommended_department": "心臟內科"
  }
}
```
