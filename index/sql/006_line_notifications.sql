BEGIN;
CREATE TABLE IF NOT EXISTS public.line_links (
 patient_id text PRIMARY KEY, line_user_id text UNIQUE NOT NULL,
 enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS public.line_link_codes (
 code_hash text PRIMARY KEY, patient_id text UNIQUE NOT NULL,
 expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS public.line_outbox (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), patient_id text NOT NULL,
 event_key text UNIQUE NOT NULL, message text NOT NULL,
 status text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz,
 last_error text
);
ALTER TABLE public.line_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.line_link_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.line_outbox ENABLE ROW LEVEL SECURITY;
CREATE OR REPLACE FUNCTION public.consume_line_code(p_hash text, p_user text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE p text;
BEGIN
 DELETE FROM line_link_codes WHERE code_hash=p_hash AND expires_at>now() RETURNING patient_id INTO p;
 IF p IS NULL THEN RETURN false; END IF;
 IF EXISTS(SELECT 1 FROM line_links WHERE line_user_id=p_user AND patient_id<>p) THEN
   RAISE EXCEPTION 'LINE_ALREADY_LINKED';
 END IF;
 INSERT INTO line_links VALUES(p,p_user,true) ON CONFLICT(patient_id)
 DO UPDATE SET line_user_id=EXCLUDED.line_user_id,enabled=true;
 RETURN true;
END $$;
CREATE OR REPLACE FUNCTION public.claim_line_messages()
RETURNS SETOF public.line_outbox LANGUAGE sql SECURITY DEFINER SET search_path=public AS $$
 UPDATE line_outbox SET status='processing',attempts=attempts+1,available_at=now()+interval '5 minutes'
 WHERE id IN (SELECT id FROM line_outbox WHERE
 ((status='pending' AND available_at<=now()) OR (status='processing' AND available_at<=now()))
 AND attempts<10 ORDER BY available_at LIMIT 50 FOR UPDATE SKIP LOCKED)
 RETURNING *;
$$;
CREATE OR REPLACE FUNCTION public.queue_booking_line() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE label text;
BEGIN
 IF TG_OP='INSERT' THEN label:='掛號成功';
 ELSIF NEW.status='cancelled' AND OLD.status IS DISTINCT FROM NEW.status THEN label:='掛號已取消';
 ELSIF (NEW.appointment_date,NEW.time_slot,NEW.doctor) IS DISTINCT FROM
       (OLD.appointment_date,OLD.time_slot,OLD.doctor) THEN label:='掛號已改期';
 ELSE RETURN NEW; END IF;
 IF EXISTS(SELECT 1 FROM line_links WHERE patient_id=NEW.patient_id::text AND enabled) THEN
 INSERT INTO line_outbox(patient_id,event_key,message) VALUES
 (NEW.patient_id::text,gen_random_uuid()::text,label||E'\n日期：'||NEW.appointment_date::text||E'\n時段：'||NEW.time_slot||E'\n醫師：'||NEW.doctor);
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS booking_line_notifications ON public.appointments;
CREATE TRIGGER booking_line_notifications AFTER INSERT OR UPDATE ON public.appointments
FOR EACH ROW EXECUTE FUNCTION public.queue_booking_line();
REVOKE ALL ON FUNCTION public.consume_line_code(text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_line_messages() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.queue_booking_line() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.consume_line_code(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_line_messages() TO service_role;
COMMIT;
