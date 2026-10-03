-- ============================================================
-- 診所自訂：問診題目（clinic_questions）與診所知識（clinic_knowledge，診所專屬 RAG）
-- 使用方式：在 Supabase 專案的 SQL Editor 貼上並執行這份檔案（重複執行也不會出錯）
-- 沒有執行這份檔案時，系統會照常使用預設題目與基礎知識庫。
-- ============================================================

-- ---------- 問診題目：診所改過的部位才會有資料，沒改過的部位用系統預設題目 ----------
create table if not exists clinic_questions (
    id uuid primary key default gen_random_uuid(),
    body_part text not null,              -- 部位（頸椎、腰椎、膝關節…，對應前端的問診部位）
    sort_order integer not null default 0,
    q_key text not null,                  -- 題目代碼（同一部位內不重複，問答紀錄用它對應題目）
    question text not null,               -- 題目文字
    options jsonb not null default '[]',  -- 快捷選項，例如 ["一天","兩天","一週"]
    created_at timestamptz not null default now()
);

comment on table clinic_questions is '診所自訂的問診題目（依醫師問診流程設定）';
create index if not exists idx_clinic_questions_body_part on clinic_questions(body_part);

-- ---------- 診所知識：跟基礎知識庫合併成 RAG 索引，問診建議與看診前摘要都會引用 ----------
create table if not exists clinic_knowledge (
    id uuid primary key default gen_random_uuid(),
    title text not null,                  -- 標題，例如「本院膝關節注射療程說明」
    body_part text,                       -- 適用部位（可空白＝不限）
    department text,                      -- 對應的問題類型代碼（可空白）
    keywords text default '',             -- 常見說法／關鍵字，越多越容易被檢索到
    content text not null,                -- 建議內容或醫師的處理原則
    warning text default '',              -- 需要特別注意的警訊
    active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

comment on table clinic_knowledge is '診所自建的骨科知識（診所專屬 RAG）';

alter table clinic_questions enable row level security;
alter table clinic_knowledge enable row level security;
