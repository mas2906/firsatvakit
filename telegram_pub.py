#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram yayıncı.
- Kanal: Tüm fırsatları kanal'a yayınlar
- Kullanıcılar: Kişisel bildirim aboneliği olanlar
"""

import os, asyncio
import httpx

CHANNEL_TOKEN   = os.getenv("TG_CHANNEL_TOKEN", "")   # Kanal botu token
CHANNEL_ID      = os.getenv("TG_CHANNEL_ID", "")      # @firsatvakti veya -100xxx

DOMAIN = "https://firsatvakti.com"

PLATFORM_EMOJI = {
    "amazon":      "🟠",
    "trendyol":    "🔴",
    "n11":         "🟣",
    "hepsiburada": "🟡",
}


def _fmt_price(v) -> str:
    if v is None: return "–"
    return f"{v:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


def build_deal_message(product: dict, new_price: float, old_price: float,
                        pct: float, short_slug: str) -> str:
    """Hem kanal hem kullanıcı bildirimleri için mesaj şablonu."""
    platform = product["platform"]
    emoji = PLATFORM_EMOJI.get(platform, "🛒")
    title = (product.get("title") or "").strip()[:80]
    short_url = f"{DOMAIN}/go/{short_slug}"
    deal_url  = f"{DOMAIN}/deal/{product.get('deal_id','')}"

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
        f"💰 <s>{_fmt_price(old_price)}</s>  →  "
        f"🔥 <b>{_fmt_price(new_price)}</b>\n\n"
        f"🔗 <a href='{short_url}'>Fırsata Git ↗</a>   "
        f"<a href='{deal_url}'>Detay</a>\n"
        f"──────────────────\n"
        f"<i>🌐 firsatvakti.com</i>"
    )
    return msg


async def _send(token: str, chat_id: str, text: str,
                image_url: str = None) -> bool:
    """Telegram mesajı gönder. Resim varsa photo olarak gönder."""
    if not token or not chat_id:
        return False

    base = f"https://api.telegram.org/bot{token}"

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            if image_url:
                r = await client.post(f"{base}/sendPhoto", json={
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                })
                if r.status_code == 200:
                    print(f"[tg] ✔ Gönderildi (photo) → {chat_id}")
                    return True
                else:
                    print(f"[tg] ⚠ sendPhoto başarısız ({r.status_code}), düz mesaja geçiliyor...")

            # Resim yoksa veya sendPhoto başarısızsa düz mesaj gönder
            r = await client.post(f"{base}/sendMessage", json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if r.status_code == 200:
                print(f"[tg] ✔ Gönderildi (text) → {chat_id}")
                return True
            else:
                print(f"[tg] ❌ {r.status_code}: {r.text[:200]}")
                return False
        except Exception as e:
            print(f"[tg] exception: {e}")
            return False


async def publish_deal(db, deal_id: int, product: dict,
                        new_price: float, old_price: float,
                        pct: float, short_slug: str):
    """
    1) Kanal'a yayınla
    2) Aboneleri bildir (ilgili platforma abone ve min_discount uyuyorsa)
    """
    msg = build_deal_message(product, new_price, old_price, pct, short_slug)
    image_url = product.get("image_url")
    platform  = product.get("platform", "")

    # 1) Kanal bildirimi
    if CHANNEL_TOKEN and CHANNEL_ID:
        await _send(CHANNEL_TOKEN, CHANNEL_ID, msg, image_url)

    # 2) Kişisel abonelikler
    subs = db.execute("""
        SELECT chat_id, min_discount_pct, platforms
        FROM tg_subscriptions
        WHERE active=1
          AND CAST(min_discount_pct AS REAL) <= ?
    """, (pct,)).fetchall()

    tasks = []
    for sub in subs:
        plats = (sub["platforms"] or "amazon,trendyol,n11").split(",")
        if platform not in plats:
            continue
        # Kullanıcı bildirimleri için kanal token'ı kullanıyoruz
        # (veya ayrı bir bot token env'den alınabilir)
        tasks.append(_send(CHANNEL_TOKEN, sub["chat_id"], msg, image_url))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        print(f"[tg] Kullanıcı bildirimleri: {ok}/{len(tasks)} başarılı")

    # Deal'ı DB'de işaretle
    db.execute("UPDATE deals SET active=1 WHERE id=?", (deal_id,))
    db.commit()
