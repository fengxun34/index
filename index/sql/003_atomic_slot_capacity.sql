-- ============================================================
-- 修正容額上限在高併發下可能被打破的問題
-- 原因：app.py 原本是「先 SELECT 數量檢查、再 INSERT」，這兩步不是同一筆
--       交易，多個掛號請求同時打進來時，可能會一起通過檢查、一起塞進去，
--       導致同一位醫師同一時段的掛號人數超過 MAX_PATIENTS_PER_SLOT。
--       這是跑壓力測試（stress_test.py）時發現的問題。
-- 解法：用 Postgres 的 advisory lock，把「檢查容額 + 新增掛號」包成同一個
--       交易內的原子操作，同一個（醫師、日期、時段）的並發請求會被序列化，
--       確保容額上限在任何併發狀況下都不會被打破。
-- 使用方式：在 Supabase 專案的 SQL Editor 貼上並執行整份檔案
-- ============================================================

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
    -- 用（醫師、日期、時段）算出一把鎖，讓同一個時段的並發請求排隊處理，
    -- 不同時段的請求則完全不受影響、照樣平行處理。
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
    '原子性地檢查容額並新增掛號，避免高併發下容額上限被打破（見 003 遷移檔說明）';
