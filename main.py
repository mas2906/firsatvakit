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
from fastapi.responses import Response
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from db import init_db
from security import get_secret_key
from routers import admin, api, auth, misc, products


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="FırsatVakti v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Middleware ────────────────────────────────────────────────
app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), max_age=86400 * 7)


@app.middleware("http")
async def csrf_cookie_middleware(request, call_next):
    response = await call_next(request)
    if "csrftoken" not in request.cookies:
        token = secrets.token_hex(32)
        response.set_cookie("csrftoken", token, samesite="lax", httponly=False, max_age=86400 * 7)
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
