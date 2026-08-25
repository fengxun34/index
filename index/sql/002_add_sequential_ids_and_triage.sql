-- ============================================================
-- 增量更新：新增病歷號／掛號序號（連續編號）與 AI 問診紀錄表
-- 用途：如果你已經執行過舊版 schema.sql、patients/appointments 表已經存在，
--       執行這份檔案來補上新欄位與新資料表，不會影響既有資料。
-- 使用方式：在 Supabase 專案的 SQL Editor 貼上並執行整份檔案
-- ============================================================

alter table patients add column if not exists patient_no bigserial unique;
comment on column patients.patient_no is '病歷號（連續編號，僅供人工對照，不作為系統識別用）';

alter table appointments add column if not exists appointment_no bigserial unique;
comment on column appointments.appointment_no is '掛號序號（連續編號，僅供人工對照）';

create table if not exists triage_records (
    id uuid primary key default gen_random_uuid(),
    appointment_id uuid references appointments(id) on delete set null,
    patient_id uuid not null references patients(id) on delete cascade,
    body_part text,
    main_complaint text,
    qa_answers jsonb,
    recommended_department text,
    ai_suggestion text,
    created_at timestamptz not null default now()
);

comment on table triage_records is 'AI 問診過程紀錄，供醫師看診前參考';

create index if not exists idx_triage_records_patient_id on triage_records(patient_id);
create index if not exists idx_triage_records_appointment_id on triage_records(appointment_id);

alter table triage_records enable row level security;
