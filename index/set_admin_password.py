"""設定／修改後台管理員登入密碼。

用法：
    python set_admin_password.py <帳號> <密碼>

會把帳號密碼寫入（或更新）Supabase 的 admin_users 表，不需要手動寫 SQL。
執行前請先在 .env 設定好 SUPABASE_URL 與 SUPABASE_SERVICE_ROLE_KEY
（跟 app.py 用的是同一組）。
"""

import hashlib
import os
import secrets
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    """雜湊密碼，回傳可直接存進 admin_users.password 欄位的字串。

    格式：pbkdf2_sha256$<迭代次數>$<salt(hex)>$<hash(hex)>
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def main():
    if len(sys.argv) != 3:
        print("用法：python set_admin_password.py <帳號> <密碼>")
        sys.exit(1)

    username, password = sys.argv[1], sys.argv[2]

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        print("❌ 尚未設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，請先設定 .env")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)

    supabase.table("admin_users").upsert(
        {"username": username, "password": hash_password(password)},
        on_conflict="username",
    ).execute()

    print(f"✅ 已設定管理員帳號「{username}」的密碼（已雜湊儲存），之後可用這組帳密登入後台掛號總覽頁面。")


if __name__ == "__main__":
    main()
