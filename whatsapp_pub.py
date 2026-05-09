#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhatsApp yayıncı — Meta WhatsApp Business API
Onaylanan fırsatları belirtilen numaraya gönderir.
"""

import os
import httpx

WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.getenv("WA_ACCESS_TOKEN", "")
WA_TO_NUMBER       = os.getenv("WA_TO_NUMBER", "")   # Mesajın gideceği numara (+905...)

DOMAIN = os.getenv("SITE_URL", "https://firsatvakti.com")

PLATFORM_EMOJI = {
    "amazon":      "🟠",
    "trendyol":    "🔴",
    "n11":         "🟣",
    "hepsiburada": "🟡",
}


def _fmt_price(v) -> str:
    if v is None:
        return "–"
    return f"{v:,.0f} TL".replace(",", ".")


async def _send_wa(to: str, text: str, image_url: str = None) -> bool:
    """Meta WhatsApp Business API ile mesaj gönder. Görsel varsa image+caption olarak gönder."""
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN or not to:
        print("[wa] ⚠ WA ayarları eksik, mesaj atlandı")
        return False

    api_url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    to_clean = to.replace("+", "").replace(" ", "")

    # Görsel varsa image mesajı dene
    if image_url:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_clean,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": text,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(api_url, headers=headers, json=payload)
            if r.status_code == 200:
                print(f"[wa] ✔ WhatsApp gönderildi (görsel) → {to}")
                return True
            else:
                print(f"[wa] ⚠ Görsel gönderilemedi ({r.status_code}), düz metin deneniyor...")
        except Exception as e:
            print(f"[wa] görsel hata: {e}")

    # Görselsiz metin mesajı
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(api_url, headers=headers, json=payload)
        if r.status_code == 200:
            print(f"[wa] ✔ WhatsApp gönderildi (metin) → {to}")
            return True
        else:
            print(f"[wa] ❌ {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"[wa] exception: {e}")
        return False


async def publish_deal_whatsapp(deal_id: int, product: dict,
                                 new_price: float, old_price: float,
                                 pct: float, short_slug: str):
    """Onaylanan fırsatı WhatsApp'a gönder — Telegram ile aynı içerik."""
    platform  = product.get("platform", "")
    emoji     = PLATFORM_EMOJI.get(platform, "🛒")
    title     = (product.get("title") or "Ürün").strip()[:80]
    short_url = f"{DOMAIN}/go/{short_slug}"
    image_url = product.get("image_url")

    rating = product.get("rating")
    reviews = product.get("review_count")
    rating_line = ""
    if rating:
        stars = "⭐" * round(rating)
        rating_line = f"\n{stars} {rating:.1f}"
        if reviews:
            rating_line += f" ({reviews:,} yorum)"

    msg = (
        f"{emoji} *%{pct:.0f} İNDİRİM!*\n\n"
        f"🛍 *{title}*"
        f"{rating_line}\n\n"
        f"💰 ~~{_fmt_price(old_price)}~~  →  🔥 *{_fmt_price(new_price)}*\n\n"
        f"👉 {short_url}\n"
        f"──────────────────\n"
        f"🌐 firsatvakti.com"
    )
    await _send_wa(WA_TO_NUMBER, msg, image_url)
