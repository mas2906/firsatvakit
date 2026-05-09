#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram yayıncı.

- Admin botu  : Onay bekleyen fırsatları adminin özel sohbetine gönderir
- Kanal botu  : Onaylanan fırsatları @firsatvakti1 kanalına yayınlar
- Aboneler    : Kanal botu ile kişisel bildirimleri iletir
"""

import logging
import os
import asyncio
import httpx

log = logging.getLogger("telegram_pub")

# ── Admin botu — sadece sana özel bildirim gönderir ─────────
ADMIN_TOKEN = os.getenv("TG_ADMIN_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("TG_ADMIN_CHAT_ID", "")

# ── Kanal botu — @firsatvakti1'e yayın yapar ────────────────
CHANNEL_TOKEN = os.getenv("TG_CHANNEL_TOKEN", "")
CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "")

DOMAIN = os.getenv("SITE_URL", "https://firsatvakti.com")

PLATFORM_EMOJI = {
    "amazon": "🟠",
    "trendyol": "🔴",
    "n11": "🟣",
    "hepsiburada": "🟡",
}


def normalize_platform(value: str) -> str:
    """
    Platform adlarını normalize eder.
    Hepsiburada için alias desteği eklendi.
    """
    p = (value or "").strip().lower()

    aliases = {
        "hb": "hepsiburada",
        "hepsi_burada": "hepsiburada",
        "hepsiburada.com": "hepsiburada",
        "hepsi burada": "hepsiburada",
        "hepsıburada": "hepsiburada",
    }

    return aliases.get(p, p)


def _fmt_price(v) -> str:
    if v is None:
        return "–"
    return f"{v:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


def build_deal_message(product: dict, new_price: float, old_price: float, pct: float, short_slug: str) -> str:
    """
    Kanal ve abone bildirimleri için mesaj şablonu.
    """
    platform = normalize_platform(product.get("platform"))
    emoji = PLATFORM_EMOJI.get(platform, "🛒")

    title = (product.get("title") or "").strip()[:80]
    short_url = f"{DOMAIN}/go/{short_slug}"
    deal_url = f"{DOMAIN}/deal/{product.get('deal_id', '')}"

    rating = product.get("rating")
    reviews = product.get("review_count")

    rating_line = ""
    if rating:
        stars = "⭐" * round(rating)
        rating_line = f"\n{stars} <b>{rating:.1f}</b>"
        if reviews:
            rating_line += f" ({reviews:,} yorum)"

    pct_str = f"%{pct:.0f}" if pct else ""

    msg = (
        f"{emoji} <b>Fiyat Düştü!{' ' + pct_str + ' İndirim' if pct_str else ''}</b>\n\n"
        f"🛍 <b>{title}</b>\n"
        f"{rating_line}\n\n"
        f"💰 <s>{_fmt_price(old_price)}</s> → "
        f"🔥 <b>{_fmt_price(new_price)}</b>\n\n"
        f"🔗 <a href='{short_url}'>Fırsata Git ↗</a> "
        f"<a href='{deal_url}'>Detay</a>\n"
        f"──────────────────\n"
        f"<i>🌐 firsatvakti.com</i>"
    )

    return msg


async def _send(token: str, chat_id: str, text: str, image_url: str = None) -> bool:
    """
    Telegram mesajı gönder.
    Resim varsa photo olarak gönderir.
    """
    if not token or not chat_id:
        return False

    base = f"https://api.telegram.org/bot{token}"

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            if image_url:
                r = await client.post(
                    f"{base}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "photo": image_url,
                        "caption": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )

                if r.status_code == 200:
                    log.info(f"[tg] ✔ Gönderildi (photo) → {chat_id}")
                    return True
                else:
                    log.warning(f"[tg] sendPhoto başarısız ({r.status_code}), text fallback")

            r = await client.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

            if r.status_code == 200:
                log.info(f"[tg] ✔ Gönderildi (text) → {chat_id}")
                return True

            log.error(f"[tg] ❌ {r.status_code}: {r.text[:300]}")
            return False

        except Exception as e:
            log.error(f"[tg] exception: {e}")
            return False


async def notify_pending_approval(
    deal_id: int,
    product: dict,
    new_price: float,
    old_price: float,
    pct: float,
    pending_count: int,
):
    """
    Admin botunu kullanarak onay bekleyen fırsatı adminin özel sohbetine gönder.
    """

    if not ADMIN_TOKEN or not ADMIN_CHAT_ID:
        log.warning("[tg] TG_ADMIN_TOKEN veya TG_ADMIN_CHAT_ID eksik")
        return

    platform = normalize_platform(product.get("platform"))
    emoji = PLATFORM_EMOJI.get(platform, "🛒")

    title = (product.get("title") or "Başlık yok").strip()[:80]
    image_url = product.get("image_url")

    admin_url = f"{DOMAIN}/admin"

    msg = (
        f"🔔 <b>Yeni Fırsat Onay Bekliyor!</b>\n\n"
        f"{emoji} <b>{platform.capitalize()}</b>\n"
        f"🛍 <b>{title}</b>\n\n"
        f"💰 <s>{_fmt_price(old_price)}</s> → 🔥 <b>{_fmt_price(new_price)}</b>\n"
        f"📉 <b>%{pct:.0f} indirim</b>\n\n"
        f"👉 <a href='{admin_url}'>Admin Paneli'nde Onayla ↗</a>\n"
        f"──────────────────\n"
        f"<i>Toplam {pending_count} ürün onay bekliyor</i>"
    )

    log.info(f"[tg-admin] pending approval gönderiliyor platform={platform}")

    await _send(ADMIN_TOKEN, ADMIN_CHAT_ID, msg, image_url)


async def publish_deal(
    db,
    deal_id: int,
    product: dict,
    new_price: float,
    old_price: float,
    pct: float,
    short_slug: str,
):
    """
    1) @firsatvakti1 kanalına yayınla
    2) Aboneleri bildir
    """

    msg = build_deal_message(product, new_price, old_price, pct, short_slug)
    image_url = product.get("image_url")

    platform = normalize_platform(product.get("platform"))

    # 1) Kanal bildirimi
    if CHANNEL_TOKEN and CHANNEL_ID:
        await _send(CHANNEL_TOKEN, CHANNEL_ID, msg, image_url)
    else:
        log.warning("[tg] Kanal token/id eksik")

    # 2) Aboneler
    subs = db.execute(
        """
        SELECT chat_id, min_discount_pct, platforms
        FROM tg_subscriptions
        WHERE active=1
        AND CAST(min_discount_pct AS REAL) <= ?
        """,
        (pct,),
    ).fetchall()

    tasks = []

    for sub in subs:
        plats = (sub["platforms"] or "amazon,trendyol,n11,hepsiburada").split(",")

        plats = [normalize_platform(x) for x in plats]

        if platform not in plats:
            continue

        tasks.append(
            _send(CHANNEL_TOKEN, sub["chat_id"], msg, image_url)
        )

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        log.info(f"[tg] Kullanıcı bildirimleri {ok}/{len(tasks)} başarılı")

    db.execute("UPDATE deals SET active=1 WHERE id=?", (deal_id,))
    db.commit()


async def send_admin_alert(message: str):
    """
    Admin'e serbest metin uyarısı gönderir.
    """
    if not ADMIN_TOKEN or not ADMIN_CHAT_ID:
        return

    await _send(
        ADMIN_TOKEN,
        ADMIN_CHAT_ID,
        f"⚠️ <b>FırsatVakti Uyarı</b>\n\n{message}",
    )