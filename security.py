#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Güvenlik modülü — FırsatVakti v2: bcrypt, rate limiting, migrasyon."""

import os, secrets, time, hashlib, re, html
from datetime import datetime
from typing import Optional

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False
    print("[security] ⚠ bcrypt yok — pip install bcrypt")


def hash_password(password: str) -> str:
    if _HAS_BCRYPT:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"sha256${salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith(("$2b$", "$2a$")):
        return _HAS_BCRYPT and bcrypt.checkpw(password.encode(), stored.encode())
    if stored.startswith("sha256$"):
        _, salt, h = stored.split("$", 2)
        return secrets.compare_digest(
            hashlib.sha256(f"{salt}:{password}".encode()).hexdigest(), h)
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), stored)


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(("$2b$", "$2a$"))


def migrate_password_on_login(db, user_id: int, plain: str):
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (hash_password(plain), user_id))
    db.commit()


MAX_ATTEMPTS, LOCKOUT_MIN = 5, 15


def check_login_allowed(email: str, db=None) -> tuple:
    """
    Brute-force koruması — önce DB tablosunu kontrol eder (restart'a dayanıklı).
    db parametresi verilmezse in-memory fallback kullanılır.
    """
    if db is not None:
        try:
            row = db.execute(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE email=? AND success=0 "
                "AND attempted_at::timestamp > NOW() - INTERVAL '15 minutes'",
                (email.lower().strip(),)
            ).fetchone()
            failed = row[0] if row else 0
            if failed >= MAX_ATTEMPTS:
                last_row = db.execute(
                    "SELECT MAX(attempted_at) FROM login_attempts "
                    "WHERE email=? AND success=0 "
                    "AND attempted_at::timestamp > NOW() - INTERVAL '15 minutes'",
                    (email.lower().strip(),)
                ).fetchone()
                if last_row and last_row[0]:
                    from datetime import datetime
                    last_dt = datetime.strptime(str(last_row[0])[:19], "%Y-%m-%d %H:%M:%S")
                    rem = int((last_dt.timestamp() + LOCKOUT_MIN * 60) - time.time())
                    if rem > 0:
                        return False, rem
            return True, 0
        except Exception:
            pass  # DB hatasında in-memory fallback'e düş

    # In-memory fallback (DB yoksa)
    _attempts: dict = getattr(check_login_allowed, "_mem", {})
    check_login_allowed._mem = _attempts
    key = email.lower().strip()
    now = time.time()
    cutoff = now - LOCKOUT_MIN * 60
    _attempts.setdefault(key, [])
    _attempts[key] = [(t, ok) for t, ok in _attempts[key] if t > cutoff]
    failed = sum(1 for _, ok in _attempts[key] if not ok)
    if failed >= MAX_ATTEMPTS:
        last = max(t for t, _ in _attempts[key])
        rem = int(last + LOCKOUT_MIN * 60 - now)
        if rem > 0:
            return False, rem
    return True, 0


def record_login_attempt(email: str, success: bool):
    _attempts: dict = getattr(check_login_allowed, "_mem", {})
    check_login_allowed._mem = _attempts
    key = email.lower().strip()
    _attempts.setdefault(key, [])
    _attempts[key].append((time.time(), success))
    if success:
        _attempts[key] = [(time.time(), True)]


def record_login_attempt_db(db, email: str, ip: str, success: bool):
    db.execute("INSERT INTO login_attempts(email,ip,success,attempted_at) VALUES(?,?,?,?)",
               (email, ip, 1 if success else 0, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()


def get_secret_key() -> str:
    key = os.getenv("SECRET_KEY", "")
    weak = ["", "firsatvakti-secret-2024", "firsatvakti-gizli-anahtar-2024", "change-me"]
    if key in weak:
        print("⚠️  SECRET_KEY zayıf! → python3 -c \"import secrets;print(secrets.token_hex(32))\"")
        key = key or secrets.token_hex(32)
    return key


def sanitize_comment(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)   # HTML tag'ları sil
    text = html.escape(text)              # Kalan < > & " ' karakterlerini escape et
    return text.strip()[:max_len]
