"""LINE webhook、一次性綁定碼與可靠通知佇列。"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
from fastapi import Header, HTTPException, Request


def valid_signature(body, signature, secret):
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return bool(secret and signature and hmac.compare_digest(expected, signature))


def line_request(path, payload, retry_key=None):
    token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
    if not token:
        raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN 尚未設定')
    headers = {'Authorization': 'Bearer ' + token}
    if retry_key:
        headers['X-Line-Retry-Key'] = retry_key
    with httpx.Client(timeout=15) as client:
        response = client.post('https://api.line.me/v2/bot/message/' + path,
                               headers=headers, json=payload)
    if retry_key and response.status_code == 409 and response.headers.get('x-line-accepted-request-id'):
        return
    response.raise_for_status()


FAIL_WINDOW = 600
MAX_FAILS_PER_USER = 5
MAX_FAILS_TOTAL = 40
_fails = []  # (時間, line_user_id)：綁定碼輸入錯誤紀錄，擋暴力猜碼


def _too_many_fails(uid):
    now = time.time()
    _fails[:] = [f for f in _fails if now - f[0] < FAIL_WINDOW]
    return len(_fails) >= MAX_FAILS_TOTAL or sum(1 for f in _fails if f[1] == uid) >= MAX_FAILS_PER_USER


def install(app, db_getter, patient_from_token):
    def database():
        db = db_getter()
        if db is None:
            raise HTTPException(503, 'Supabase 尚未設定')
        return db

    def patient(auth):
        p = patient_from_token(auth)
        if not p:
            raise HTTPException(401, '請先登入病患帳號')
        return p

    @app.get('/api/line/status')
    def line_status(authorization: str = Header(default='')):
        p = patient(authorization)
        rows = database().table('line_links').select('enabled').eq('patient_id', str(p['id'])).execute().data
        return {'status': 'success', 'linked': bool(rows), 'enabled': bool(rows and rows[0]['enabled'])}

    @app.post('/api/line/link-code')
    def create_code(authorization: str = Header(default='')):
        p = patient(authorization)
        if not os.getenv('LINE_CHANNEL_SECRET') or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
            raise HTTPException(503, '診所尚未設定 LINE Bot')
        code = secrets.token_hex(4).upper()  # 8 碼，方便手機輸入；靠 10 分鐘有效期與錯誤次數限制防猜
        database().table('line_link_codes').upsert({
            'patient_id': str(p['id']), 'code_hash': hashlib.sha256(code.encode()).hexdigest(),
            'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        }, on_conflict='patient_id').execute()
        return {'status': 'success', 'code': code, 'expires_minutes': 10,
                'message': '加診所 LINE 好友後，於一對一聊天傳送：綁定 ' + code,
                'friend_url': os.getenv('LINE_FRIEND_URL', '')}

    @app.delete('/api/line/link')
    def unlink(authorization: str = Header(default='')):
        p = patient(authorization)
        db = database()
        db.table('line_links').delete().eq('patient_id', str(p['id'])).execute()
        db.table('line_link_codes').delete().eq('patient_id', str(p['id'])).execute()
        db.table('line_outbox').update({'status': 'skipped'}).eq('patient_id', str(p['id'])).in_('status', ['pending', 'processing']).execute()
        return {'status': 'success'}

    @app.post('/api/line/webhook')
    async def webhook(request: Request):
        raw = await request.body()
        secret = os.getenv('LINE_CHANNEL_SECRET', '')
        if not secret:
            raise HTTPException(503, 'LINE 尚未設定')
        if not valid_signature(raw, request.headers.get('x-line-signature', ''), secret):
            raise HTTPException(401, '簽章不符')
        try:
            events = json.loads(raw).get('events', [])
        except ValueError:
            raise HTTPException(400, 'JSON 格式錯誤')
        db = database()
        for event in events:
            source = event.get('source', {})
            # 不接受群組綁定，避免病患資訊出現在群組。
            if source.get('type') != 'user' or not source.get('userId'):
                continue
            uid = source['userId']
            if event.get('type') == 'unfollow':
                db.table('line_links').update({'enabled': False}).eq('line_user_id', uid).execute()
                continue
            msg = event.get('message', {})
            if event.get('type') != 'message' or msg.get('type') != 'text':
                continue
            text = msg.get('text', '').strip()
            reply = '請先在病患端登入，按「綁定 LINE 通知」取得綁定碼。'
            if text.startswith('綁定 '):
                code = text.split(' ', 1)[1].strip().upper()
                if _too_many_fails(uid):
                    line_request('reply', {'replyToken': event['replyToken'], 'messages': [{'type': 'text', 'text': '嘗試次數過多，請 10 分鐘後再試。'}]}) if event.get('replyToken') else None
                    continue
                try:
                    ok = db.rpc('consume_line_code', {'p_hash': hashlib.sha256(code.encode()).hexdigest(), 'p_user': uid}).execute().data
                    if not ok:
                        _fails.append((time.time(), uid))
                    reply = '綁定成功！將收到掛號資訊與預約前一天提醒。' if ok else '綁定碼無效或已過期，請回病患端重新取得。'
                except Exception as exc:
                    if 'LINE_ALREADY_LINKED' not in str(exc):
                        raise
                    reply = '這個 LINE 已綁定其他病患，請先解除原帳號綁定。'
            elif text == '停止提醒':
                db.table('line_links').update({'enabled': False}).eq('line_user_id', uid).execute()
                reply = '已停止提醒；重新取得綁定碼並綁定即可開啟。'
            elif text == '開啟提醒':
                rows = db.table('line_links').update({'enabled': True}).eq('line_user_id', uid).execute().data
                reply = '已開啟提醒。' if rows else '請先在病患端綁定帳號。'
            if event.get('replyToken'):
                line_request('reply', {'replyToken': event['replyToken'], 'messages': [{'type': 'text', 'text': reply}]})
        return {'status': 'ok'}


def run_jobs(db, now=None):
    """每分鐘呼叫；台灣時間 09:00 後建立明日提醒，佇列以原子租約取件。"""
    now = now or datetime.now(ZoneInfo('Asia/Taipei'))
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    if now.hour >= 9:
        appts = db.table('appointments').select('id,patient_id,doctor,appointment_date,time_slot').eq('status', 'confirmed').eq('appointment_date', tomorrow).execute().data or []
        for a in appts:
            links = db.table('line_links').select('enabled').eq('patient_id', str(a['patient_id'])).execute().data
            if not links or not links[0]['enabled']:
                continue
            key = f"reminder:{a['id']}:{a['appointment_date']}:{a['time_slot']}:{a['doctor']}"
            db.table('line_outbox').upsert({'patient_id': str(a['patient_id']), 'event_key': key,
                'message': f"明日門診提醒\n日期：{tomorrow}\n時段：{a['time_slot']}\n醫師：{a['doctor']}"},
                on_conflict='event_key', ignore_duplicates=True).execute()
    jobs = db.rpc('claim_line_messages').execute().data or []
    for job in jobs:
        try:
            links = db.table('line_links').select('line_user_id,enabled').eq('patient_id', job['patient_id']).execute().data
            if not links or not links[0]['enabled']:
                db.table('line_outbox').update({'status': 'skipped'}).eq('id', job['id']).execute()
                continue
            if job['event_key'].startswith('reminder:'):
                # 改期／取消後不發送舊的提醒。
                a_id = job['event_key'].split(':')[1]
                rows = db.table('appointments').select('id,doctor,appointment_date,time_slot,status').eq('id', a_id).execute().data
                a = rows[0] if rows else None
                key = f"reminder:{a['id']}:{a['appointment_date']}:{a['time_slot']}:{a['doctor']}" if a else ''
                if not a or a['status'] != 'confirmed' or key != job['event_key'] or a['appointment_date'] != tomorrow:
                    db.table('line_outbox').update({'status': 'skipped'}).eq('id', job['id']).execute()
                    continue
            line_request('push', {'to': links[0]['line_user_id'], 'messages': [{'type': 'text', 'text': job['message']}]}, job['id'])
            db.table('line_outbox').update({'status': 'sent', 'sent_at': datetime.now(timezone.utc).isoformat(), 'last_error': None}).eq('id', job['id']).execute()
        except Exception as exc:
            # 不存 token、病患資訊或 HTTP 原始回應。
            db.table('line_outbox').update({'status': 'failed' if job['attempts'] >= 10 else 'pending',
                'available_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                'last_error': type(exc).__name__}).eq('id', job['id']).execute()
