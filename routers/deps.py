"""Paylaşılan yardımcı fonksiyonlar ve durum — tüm router'lar buradan import eder."""
import os
import secrets
import time
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from config import INDEX_CACHE_TTL_SECS, RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECS
from db import get_db

templates = Jinja2Templates(directory="templates")


def _time_ago(value):
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        diff = int((datetime.utcnow() - dt).total_seconds())
        if diff < 60:
            return "az önce kontrol edildi"
        if diff < 3600:
            return f"{diff//60} dk önce kontrol edildi"
        if diff < 86400:
            return f"{diff//3600} sa önce kontrol edildi"
        return f"{diff//86400} gün önce kontrol edildi"
    except Exception:
        return ""


def _hours_since(value):
    if not value:
        return 9999
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return int((datetime.utcnow() - dt).total_seconds() / 3600)
    except Exception:
        return 9999


def _price_tr(value):
    """3798.9 → '3.798,90'  (Türk fiyat formatı)"""
    try:
        v = float(value)
        formatted = f"{v:,.2f}"
        return formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return value


templates.env.filters["time_ago"] = _time_ago
templates.env.filters["hours_since"] = _hours_since
templates.env.filters["price_tr"] = _price_tr

# ── Rate limiting (in-memory, IP bazlı) ──────────────────────
_rate_store: dict = {}
_RATE_CLEANUP_INTERVAL = 300
_rate_last_cleanup = 0.0


def _is_rate_limited(ip: str, action: str) -> bool:
    global _rate_last_cleanup
    limit = RATE_LIMIT_MAX_REQUESTS.get(action, 10)
    window = RATE_LIMIT_WINDOW_SECS
    now = time.time()
    key = f"{ip}:{action}"
    _rate_store.setdefault(key, [])
    _rate_store[key] = [t for t in _rate_store[key] if now - t < window]
    if len(_rate_store[key]) >= limit:
        return True
    _rate_store[key].append(now)
    if not _rate_store[key]:
        del _rate_store[key]
        return False
    if now - _rate_last_cleanup > _RATE_CLEANUP_INTERVAL:
        _rate_last_cleanup = now
        stale = [k for k, ts in _rate_store.items() if not any(now - t < window for t in ts)]
        for k in stale:
            del _rate_store[k]
    return False


# ── CSRF doğrulama ────────────────────────────────────────────
def _verify_csrf(request: Request, csrf_token: str) -> bool:
    cookie = request.cookies.get("csrftoken", "")
    if not cookie or not csrf_token:
        return False
    return secrets.compare_digest(cookie, csrf_token)


async def require_csrf(request: Request) -> None:
    """
    Admin router'ındaki tüm POST/PUT/PATCH/DELETE isteklerine uygulanan CSRF guard.
    İki yol kabul edilir (öncelik sırasıyla):
      1. X-CSRFToken başlığı — JavaScript/fetch tabanlı istekler için
      2. Origin/Referer başlığı — token yoksa güvenilir kaynak kontrolü
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    header_token = request.headers.get("X-CSRFToken", "")
    if header_token:
        if not _verify_csrf(request, header_token):
            raise HTTPException(403, "Geçersiz CSRF token")
        return
    # Header yoksa Origin/Referer ile kaynak doğrula
    site = os.getenv("SITE_URL", "https://firsatvakti.com")
    origin  = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if origin and not origin.startswith(site):
        raise HTTPException(403, "Geçersiz istek kaynağı (CSRF)")
    if not origin and not referer.startswith(site):
        raise HTTPException(403, "Geçersiz istek kaynağı (CSRF)")


# ── Ana sayfa cache (TTL bazlı) ───────────────────────────────
_index_cache: dict = {}


def _cache_get(key: str):
    entry = _index_cache.get(key)
    if entry and (time.time() - entry["ts"]) < INDEX_CACHE_TTL_SECS:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _index_cache[key] = {"data": data, "ts": time.time()}


def cache_invalidate_index():
    """Deal onaylandığında veya reddedildiğinde tüm index cache'ini temizle."""
    keys = [k for k in _index_cache if k.startswith("index_")]
    for k in keys:
        del _index_cache[k]


# ── Sorgu yardımcıları ────────────────────────────────────────
_GROUP_CHEAPEST_FILTER = (
    " AND NOT EXISTS ("
    "SELECT 1 FROM product_group_members pgm_m"
    " JOIN product_group_members pgm_o ON pgm_o.group_id=pgm_m.group_id AND pgm_o.product_id!=d.product_id"
    " JOIN (SELECT product_id,MAX(id) as max_id FROM deals where active=1 GROUP BY product_id) bo ON bo.product_id=pgm_o.product_id"
    " JOIN deals d_o ON d_o.id=bo.max_id"
    " where pgm_m.product_id=d.product_id AND d_o.new_price<d.new_price)"
)


def get_ticker_items(db):
    return db.execute("""
        SELECT d.discount_pct, d.new_price, p.title, p.platform
        FROM deals d JOIN products p ON d.product_id = p.id
        INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id = best.max_id
        ORDER BY d.created_at DESC LIMIT 10
    """).fetchall()


def now_str() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def current_user(request: Request) -> Optional[dict]:
    uid = request.session.get("user_id") if hasattr(request, "session") else None
    if not uid:
        return None
    return get_db().execute(
        "SELECT id, username, email, role FROM users WHERE id=?", (uid,)
    ).fetchone()


def require_admin(request: Request):
    user = current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Erişim yok")
    return user
