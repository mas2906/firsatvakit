#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Anahtar kelimeli fiyat alarmları (yeni tema, 2026-10-13).

Kullanıcı "airfryer", "Dyson", "Karaca" gibi bir kelime ve en az indirim oranı
girer. Başlığında kelime geçen bir ürün o oranla YAYINLANINCA (admin onayı →
telegram_pub.publish_deal) kullanıcıya Telegram'dan (notify_settings'te
tanımlıysa), yoksa e-postayla haber verilir. Aynı alarm aynı fırsat için bir kez.
"""

import asyncio
import html
import logging
import os
from datetime import datetime

log = logging.getLogger("main")

MAX_ALERTS_PER_USER = 20
MIN_PCT_OPTIONS = (10, 20, 30)


def _norm(s: str) -> str:
    """Türkçe büyük/küçük harf duyarsız karşılaştırma için."""
    return (s or "").replace("I", "ı").replace("İ", "i").lower().strip()


def matching_alerts(db, title: str, pct: float) -> list:
    """Başlığa ve indirime uyan alarmlar (kullanıcı bilgisiyle)."""
    t = _norm(title)
    if not t:
        return []
    rows = db.execute(
        "SELECT a.id, a.user_id, a.keyword, a.min_pct, u.email, u.username, "
        "       n.tg_chat_id, n.tg_active "
        "FROM price_alerts a JOIN users u ON u.id = a.user_id "
        "LEFT JOIN notify_settings n ON n.user_id = a.user_id "
        "WHERE a.min_pct <= ?", (pct,)
    ).fetchall()
    return [r for r in rows if _norm(r["keyword"]) and _norm(r["keyword"]) in t]


async def notify_price_alerts(db, deal_id: int, product: dict, new_price: float,
                              old_price: float, pct: float) -> int:
    """publish_deal'dan çağrılır. Gönderilen bildirim sayısını döner."""
    from email_utils import send_price_alert
    from telegram_pub import CHANNEL_TOKEN, _send

    site = os.getenv("SITE_URL", "https://firsatvakti.com")
    deal_url = f"{site}/deal/{deal_id}"
    title = product.get("title") or ""
    sent = 0
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for a in matching_alerts(db, title, pct):
        try:
            cur = db.execute(
                "INSERT INTO price_alert_hits(alert_id, deal_id, sent_at) VALUES(?,?,?) "
                "ON CONFLICT DO NOTHING", (a["id"], deal_id, now))
            if not cur.rowcount:
                continue  # bu alarm bu fırsat için zaten bildirildi
            ok = False
            if a["tg_chat_id"] and a["tg_active"] and CHANNEL_TOKEN:
                msg = (f"🔔 <b>Fiyat alarmın:</b> “{html.escape(a['keyword'])}”\n\n"
                       f"{html.escape(title[:120])}\n"
                       f"<b>{new_price:,.2f} TL</b> (−%{pct:.0f})\n{deal_url}")
                ok = await _send(CHANNEL_TOKEN, a["tg_chat_id"], msg, product.get("image_url"))
            if not ok and a["email"]:
                ok = await asyncio.to_thread(
                    send_price_alert, to=a["email"], username=a["username"],
                    product_title=title, old_price=old_price, new_price=new_price,
                    pct=pct, deal_url=deal_url)
            db.execute("UPDATE price_alerts SET last_notified_at=? WHERE id=?", (now, a["id"]))
            sent += bool(ok)
        except Exception as e:
            log.warning(f"[alarm] #{a['id']} bildirim hatası: {e}")
    db.commit()
    if sent:
        log.info(f"[alarm] fırsat #{deal_id}: {sent} fiyat alarmı bildirildi")
    return sent
