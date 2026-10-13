#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Günlük eşitleme işlerinin (karaca_discovery, beymen_sync) durum kaydı.

settings tablosunda `<ad>_status` anahtarında JSON tutulur; admin "Scraper Durumu"
paneli (/api/scraper-monitor) buradan okur. Yazma hatası eşitlemeyi asla durdurmaz.
"""

import json
from datetime import datetime, timezone

from db import get_db


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def set_status(name: str, **fields) -> None:
    try:
        db = get_db()
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"{name}_status",)).fetchone()
        cur = {}
        if row and row["value"]:
            try:
                cur = json.loads(row["value"])
            except Exception:
                cur = {}
        cur.update(fields)
        ts = now()
        db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (f"{name}_status", json.dumps(cur, ensure_ascii=False), ts)
        )
        db.commit()
    except Exception:
        pass


def start(name: str) -> None:
    """Yeni çalışma: önceki ilerleme alanlarını sıfırla, son sonuçları (last_stats) koru."""
    try:
        db = get_db()
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"{name}_status",)).fetchone()
        last_stats = None
        if row and row["value"]:
            try:
                last_stats = json.loads(row["value"]).get("last_stats")
            except Exception:
                pass
        db.execute("DELETE FROM settings WHERE key=?", (f"{name}_status",))
        db.commit()
    except Exception:
        last_stats = None
    set_status(name, state="running", phase="başlıyor", started_at=now(), finished_at=None,
               done=0, total=None, last_stats=last_stats, error=None)


def finish(name: str, stats: dict) -> None:
    set_status(name, state="done", phase="bitti", finished_at=now(), last_stats=stats)


def fail(name: str, err: str) -> None:
    set_status(name, state="error", phase="hata", finished_at=now(), error=err[:300])


def get_status(name: str) -> dict:
    try:
        row = get_db().execute("SELECT value FROM settings WHERE key=?", (f"{name}_status",)).fetchone()
        return json.loads(row["value"]) if row and row["value"] else {}
    except Exception:
        return {}
