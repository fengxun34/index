import pandas as pd
import sqlite3
import json
import pickle
import os
from sklearn.feature_extraction.text import TfidfVectorizer

def update_system():
    print("--- 🚀 開始更新 SQL 知識庫 ---")
    
    # 🌟 確保絕對路徑，防止讀錯資料夾
    base_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(base_dir, "medical_sheet.csv")
    db_path = os.path.join(base_dir, "vector_store.db")
    pkl_path = os.path.join(base_dir, "vectorizer.pkl")

    # 1. 讀取 CSV (🌟 自動處理 Windows Excel 中文編碼問題)
    try:
        df = pd.read_csv(csv_path, encoding='utf-8-sig') # 先嘗試標準 UTF-8
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(csv_path, encoding='big5')  # 失敗的話改用台灣常用的 Big5
        except Exception as e:
            print(f"❌ 讀取失敗，請確認檔案格式: {e}")
            return
    except Exception as e:
        print(f"❌ 找不到 medical_sheet.csv: {e}")
        return

    print(f"✅ 成功讀取 CSV，開始處理 {len(df)} 筆資料...")

    knowledge_data = []
    for _, row in df.iterrows():
        # 組合內容
        full_content = f"部位：{row['body_part']}。症狀：{row['keywords']}。建議：{row['advice']}。看診醫師與時段：{row['doctor_time']}。警告：{row['warning']}"
        knowledge_data.append({
            "dept": row['department'],
            "content": full_content
        })

    # 2. 訓練向量模型
    texts = [d['content'] for d in knowledge_data]
    vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer='char_wb')
    vectors = vectorizer.fit_transform(texts)

    # 儲存模型
    with open(pkl_path, "wb") as f:
        pickle.dump(vectorizer, f)

    # 3. 寫入 SQLite
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS medical_chunks")
    cursor.execute("""
        CREATE TABLE medical_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT,
            content TEXT,
            vector_json TEXT
        )
    """)

    for i, doc in enumerate(knowledge_data):
        vec_list = vectors[i].toarray()[0].tolist()
        cursor.execute(
            "INSERT INTO medical_chunks (department, content, vector_json) VALUES (?, ?, ?)",
            (doc['dept'], doc['content'], json.dumps(vec_list))
        )
    
    conn.commit()
    conn.close()
    print(f"✅ SQL 資料庫更新完成！已經將最新的 {len(knowledge_data)} 筆資料存入：\n{db_path}")

if __name__ == "__main__":
    update_system()