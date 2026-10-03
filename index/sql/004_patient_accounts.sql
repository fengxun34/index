-- ============================================================
-- 病患帳號：在 patients 表加上登入密碼欄位
-- 使用方式：在 Supabase 專案的 SQL Editor 貼上並執行這份檔案（重複執行也不會出錯）
-- ============================================================

alter table patients add column if not exists password_hash text;

comment on column patients.password_hash is
    '病患帳號登入密碼（PBKDF2-SHA256 雜湊，格式 pbkdf2_sha256$迭代次數$salt$hash）；沒有建立帳號的病人為 null';

-- 用手機號碼登入時會用到
create index if not exists idx_patients_phone on patients(phone);
