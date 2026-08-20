"""Kimlik doğrulama: kayıt, giriş, çıkış, Google OAuth, şifre sıfırlama."""
import os
import secrets
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from db import get_db
from email_utils import send_password_reset
from security import (check_login_allowed, hash_password, migrate_password_on_login,
                      needs_rehash, record_login_attempt, record_login_attempt_db,
                      verify_password)

from .deps import _is_rate_limited, _verify_csrf, current_user, get_client_ip, now_str, templates

router = APIRouter()

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _google_redirect_uri(request: Request) -> str:
    base = os.getenv("BASE_URL", "https://firsatvakti.com")
    return f"{base}/auth/google/callback"


@router.get("/register")
async def register_get(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "register"})


@router.post("/register")
async def register_post(request: Request, username: str = Form(...), email: str = Form(...),
                        password: str = Form(...), csrf_token: str = Form("")):
    ip = get_client_ip(request)
    if _is_rate_limited(ip, "register"):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register",
                                                        "error": "Çok fazla deneme. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register",
                                                        "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    email = email.strip().lower()
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register",
                                                        "error": "Bu e-posta zaten kayıtlı."})
    if len(password) < 6:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register",
                                                        "error": "Şifre en az 6 karakter olmalı."})
    cur = db.execute(
        "INSERT INTO users(username,email,password_hash,created_at,role) VALUES(?,?,?,?,?)",
        (username, email, hash_password(password), now_str(), "user")
    )
    db.commit()
    request.session["user_id"] = cur.lastrowid
    return RedirectResponse("/", status_code=302)


@router.get("/login")
async def login_get(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "login"})


@router.post("/login")
async def login_post(request: Request, email: str = Form(...), password: str = Form(...),
                     csrf_token: str = Form("")):
    db = get_db()
    ip = get_client_ip(request)
    if _is_rate_limited(ip, "login"):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
                                                        "error": "Çok fazla deneme. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
                                                        "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    email = email.strip().lower()
    allowed, wait = check_login_allowed(email, db)
    if not allowed:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
                                                        "error": f"Çok fazla hatalı deneme. {wait//60} dk sonra tekrar dene."})
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if user and not user["password_hash"]:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
                                                        "error": "Bu hesap Google ile oluşturulmuş. Aşağıdaki Google butonu ile giriş yap."})
    if not user or not verify_password(password, user["password_hash"]):
        record_login_attempt(email, False)
        record_login_attempt_db(db, email, ip, False)
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
                                                        "error": "E-posta veya şifre hatalı."})
    record_login_attempt(email, True)
    record_login_attempt_db(db, email, ip, True)
    if needs_rehash(user["password_hash"]):
        migrate_password_on_login(db, user["id"], password)
    request.session["user_id"] = user["id"]
    redirect_to = "/admin" if user["role"] == "admin" else "/"
    return RedirectResponse(redirect_to, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@router.get("/auth/google")
async def google_login(request: Request):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return RedirectResponse("/login?error=google_not_configured", status_code=302)
    state = secrets.token_hex(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": client_id,
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    from urllib.parse import urlencode
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/login", status_code=302)
    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/login", status_code=302)
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_r = await client.post(_GOOGLE_TOKEN_URL, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": _google_redirect_uri(request), "grant_type": "authorization_code",
            })
            token_r.raise_for_status()
            access_token = token_r.json().get("access_token")
            info_r = await client.get(_GOOGLE_USERINFO_URL,
                                      headers={"Authorization": f"Bearer {access_token}"})
            info_r.raise_for_status()
            info = info_r.json()
    except Exception:
        return RedirectResponse("/login", status_code=302)
    google_id = info.get("id")
    email = info.get("email", "")
    name = info.get("name") or email.split("@")[0]
    if not google_id or not email:
        return RedirectResponse("/login", status_code=302)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
    if not user:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            db.execute("UPDATE users SET google_id=? WHERE id=?", (google_id, user["id"]))
            db.commit()
        else:
            cur = db.execute(
                "INSERT INTO users(username,email,password_hash,google_id,role,created_at) VALUES(?,?,?,?,?,?)",
                (name, email, "", google_id, "user", now_str())
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    request.session["user_id"] = user["id"]
    redirect_to = "/admin" if user["role"] == "admin" else "/"
    return RedirectResponse(redirect_to, status_code=302)


@router.get("/forgot-password")
async def forgot_password_get(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@router.post("/forgot-password")
async def forgot_password_post(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("forgot_password.html", {"request": request,
                                                                    "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if user:
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO password_reset_tokens(user_id,token,expires_at,created_at) VALUES(?,?,?,?)",
            (user["id"], token, expires, now_str())
        )
        db.commit()
        send_password_reset(user["email"], user["username"], token)
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "success": "Eğer bu e-posta kayıtlıysa sıfırlama linki gönderildi. Gelen kutunu kontrol et.",
    })


@router.get("/reset-password/{token}")
async def reset_password_get(request: Request, token: str):
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now_str())
    ).fetchone()
    if not row:
        return templates.TemplateResponse("reset_password.html", {"request": request, "invalid": True})
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


@router.post("/reset-password/{token}")
async def reset_password_post(request: Request, token: str,
                               password: str = Form(...), password2: str = Form(...),
                               csrf_token: str = Form("")):
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token,
                                                                   "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now_str())
    ).fetchone()
    if not row:
        return templates.TemplateResponse("reset_password.html", {"request": request, "invalid": True})
    if len(password) < 6:
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token,
                                                                   "error": "Şifre en az 6 karakter olmalı."})
    if password != password2:
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token,
                                                                   "error": "Şifreler eşleşmiyor."})
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), row["user_id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
    db.commit()
    return templates.TemplateResponse("reset_password.html", {"request": request, "done": True})
