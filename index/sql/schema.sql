-- ============================================================
-- 骨科智慧語音掛號助理 - Supabase 資料庫結構
-- 用途：儲存病人基本資料 (patients) 與掛號紀錄 (appointments)
-- 使用方式：在 Supabase 專案的 SQL Editor 貼上並執行整份檔案
-- ============================================================

create extension if not exists pgcrypto;

-- ---------- 病人資料表 ----------
create table if not exists patients (
    id uuid primary key default gen_random_uuid(),
    patient_no bigserial unique,          -- 病歷號：依建立順序連續編號，方便人工對照查詢
    id_number text not null unique,       -- 身分證字號，作為病人唯一識別
    name text not null,
    birth_date date,
    phone text,
    password_hash text,                   -- 病患帳號登入密碼（雜湊），沒有建立帳號為 null
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

comment on table patients is '病人基本資料';
comment on column patients.id_number is '身分證字號（唯一）';
comment on column patients.patient_no is '病歷號（連續編號，僅供人工對照，不作為系統識別用）';

-- ---------- 掛號紀錄表 ----------
create table if not exists appointments (
    id uuid primary key default gen_random_uuid(),
    appointment_no bigserial unique,      -- 掛號序號：依建立順序連續編號
    patient_id uuid not null references patients(id) on delete cascade,
    department text not null check (department in (
        '脊椎外科', '運動醫學科', '關節重建科', '手外科',
        '足踝外科', '骨折創傷科', '骨質疏鬆症門診',
        '兒童骨科', '骨骼腫瘤科', '高壓氧治療中心'
    )),
    doctor text not null,
    appointment_date date not null,
    time_slot text not null,
    status text not null default 'confirmed' check (status in ('confirmed', 'cancelled', 'completed')),
    created_at timestamptz not null default now()
);

comment on table appointments is '骨科門診掛號紀錄';
comment on column appointments.department is '骨科次專科門診';
comment on column appointments.appointment_no is '掛號序號（連續編號，僅供人工對照）';

create index if not exists idx_appointments_patient_id on appointments(patient_id);
create index if not exists idx_appointments_date on appointments(appointment_date);
create index if not exists idx_patients_id_number on patients(id_number);
create index if not exists idx_patients_phone on patients(phone);

-- ---------- AI 問診紀錄表 ----------
-- 記錄使用者在問診流程中回答的內容與 AI 分流建議，供醫師看診前參考。
create table if not exists triage_records (
    id uuid primary key default gen_random_uuid(),
    appointment_id uuid references appointments(id) on delete set null,
    patient_id uuid not null references patients(id) on delete cascade,
    body_part text,                  -- 問診時選擇／偵測到的部位
    main_complaint text,             -- 主訴症狀原文
    qa_answers jsonb,                -- 問診各題的完整問答內容
    recommended_department text,     -- AI 建議掛號的骨科次專科
    ai_suggestion text,              -- RAG 檢索後給出的建議文字
    created_at timestamptz not null default now()
);

comment on table triage_records is 'AI 問診過程紀錄，供醫師看診前參考';

create index if not exists idx_triage_records_patient_id on triage_records(patient_id);
create index if not exists idx_triage_records_appointment_id on triage_records(appointment_id);

-- ---------- 後台管理員帳號表 ----------
-- 密碼請用 set_admin_password.py 設定，資料庫只存 PBKDF2-SHA256 雜湊值，不存密碼原文。
create table if not exists admin_users (
    id uuid primary key default gen_random_uuid(),
    username text not null unique,
    password text not null,               -- 密碼雜湊（格式：pbkdf2_sha256$迭代次數$salt$hash），由 set_admin_password.py 寫入
    created_at timestamptz not null default now()
);

comment on table admin_users is '後台掛號總覽頁面的管理員登入帳號';

-- ---------- Row Level Security ----------
-- 本系統的掛號 API 皆由後端 (FastAPI) 使用 Service Role Key 存取，
-- Service Role 會自動略過 RLS，因此這裡預設「不開放任何公開存取權限」，
-- 確保病人個資只能透過後端服務存取，不會被前端直接讀取。
alter table patients enable row level security;
alter table appointments enable row level security;
alter table admin_users enable row level security;
alter table triage_records enable row level security;

-- 更新 updated_at 欄位的觸發器
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_patients_updated_at on patients;
create trigger trg_patients_updated_at
    before update on patients
    for each row
    execute function set_updated_at();

-- ---------- 原子性掛號（避免高併發下容額上限被打破，詳見 sql/003_atomic_slot_capacity.sql） ----------
create or replace function book_appointment_slot(
    p_patient_id uuid,
    p_department text,
    p_doctor text,
    p_appointment_date date,
    p_time_slot text,
    p_max_per_slot integer
)
returns appointments
language plpgsql
as $$
declare
    v_lock_key bigint;
    v_current_count integer;
    v_new_row appointments;
begin
    v_lock_key := hashtextextended(p_doctor || '|' || p_appointment_date::text || '|' || p_time_slot, 0);
    perform pg_advisory_xact_lock(v_lock_key);

    select count(*) into v_current_count
    from appointments
    where doctor = p_doctor
      and appointment_date = p_appointment_date
      and time_slot = p_time_slot
      and status = 'confirmed';

    if v_current_count >= p_max_per_slot then
        raise exception 'SLOT_FULL' using errcode = 'P0001';
    end if;

    insert into appointments (patient_id, department, doctor, appointment_date, time_slot)
    values (p_patient_id, p_department, p_doctor, p_appointment_date, p_time_slot)
    returning * into v_new_row;

    return v_new_row;
end;
$$;

comment on function book_appointment_slot is
    '原子性地檢查容額並新增掛號，避免高併發下容額上限被打破';

-- ---------- 設定後台管理員帳號 ----------
-- 執行完這份 schema.sql 後，不需要在這裡手動寫 SQL 設定密碼，
-- 改用專案根目錄的 set_admin_password.py，例如：
--   python set_admin_password.py admin 你的密碼

-- ---------- 診所自訂：問診題目與診所知識（詳見 sql/005_clinic_customization.sql） ----------
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
