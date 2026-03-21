#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Güvenlik modülü — FırsatVakti v2: bcrypt, rate limiting, migrasyon."""

import os, secrets, time, hashlib, re
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


_attempts: dict = {}
MAX_ATTEMPTS, LOCKOUT_MIN = 5, 15


def check_login_allowed(email: str) -> tuple:
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
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()[:max_len]
