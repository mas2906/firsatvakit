#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firsatvakti.com — Ana FastAPI uygulaması v2"""
import os
import secrets

from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from db import init_db
from security import get_secret_key
from routers import admin, api, auth, misc, products


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    await asyncio.to_thread(init_db)
    yield


app = FastAPI(title="FırsatVakti v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Middleware ────────────────────────────────────────────────
app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), max_age=86400 * 7)


@app.middleware("http")
async def canonical_host_middleware(request, call_next):
    """http://… ve www.… varyantlarını https://firsatvakti.com'a 301'le.
    Aksi halde Google aynı sayfanın 4 kopyasını (http/https × www/apex) görüyor.
    Sadece Cloudflare üzerinden gelen isteklere uygulanır (CF-Visitor başlığı):
    scraper VPS'e doğrudan http://IP ile bağlanıyor, ona dokunulmamalı."""
    cf_visitor = request.headers.get("cf-visitor")
    if cf_visitor and request.method in ("GET", "HEAD"):
        host = request.headers.get("host", "").lower()
        is_http = '"http"' in cf_visitor.replace(" ", "")
        if host == "www.firsatvakti.com" or (is_http and host == "firsatvakti.com"):
            target = "https://firsatvakti.com" + request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(target, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def csrf_cookie_middleware(request, call_next):
    # Token'ı yanıt üretilmeden ÖNCE belirle ve request.state'e koy: şablonlar
    # formlara sunucu tarafında basabilsin. Sadece JS'in cookie'den okumasına
    # güvenmek bazı mobil/uygulama-içi tarayıcılarda boş token → "Geçersiz istek"
    # hatasına yol açıyordu.
    token = request.cookies.get("csrftoken")
    is_new = not token
    if is_new:
        token = secrets.token_hex(32)
    request.state.csrf_token = token
    response = await call_next(request)
    if is_new:
        response.set_cookie("csrftoken", token, samesite="lax", httponly=False, max_age=86400 * 30)
    return response


# ── Router'lar ────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(products.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(misc.router)

# ── Google Search Console domain doğrulama ────────────────────
_gsc_token = os.getenv("GOOGLE_SITE_VERIFICATION", "")
if _gsc_token:
    @app.get(f"/{_gsc_token}")
    async def google_site_verification():
        token = _gsc_token.replace(".html", "")
        return Response(content=f"google-site-verification: {token}", media_type="text/html")

# ── Başlangıç uyarıları ───────────────────────────────────────
if not os.getenv("SCRAPER_SERVICE_KEY"):
    print("⚠️  SCRAPER_SERVICE_KEY tanımlı değil — /api/pending-jobs herkese açık!")
if not os.getenv("FRONTEND_WEBHOOK_KEY"):
    print("⚠️  FRONTEND_WEBHOOK_KEY tanımlı değil — /api/scraper-webhook herkese açık!")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
