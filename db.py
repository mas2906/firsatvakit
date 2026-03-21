#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Veritabanı şeması v2 — çapraz platform, yorumlar, SEO, güvenlik."""

import sqlite3, os
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "firsatvakti.db")
_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
    return _conn


def init_db():
    db = get_db()
    db.executescript("""
    -- ══ MEVCUT TABLOLAR ══════════════════════════════════════
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL, asin_or_id TEXT,
        source_url TEXT UNIQUE NOT NULL,
        title TEXT, image_url TEXT, description TEXT,
        rating REAL, review_count INTEGER,
        brand TEXT, category TEXT, barcode TEXT,
        stock TEXT DEFAULT 'unknown',
        first_seen_at TEXT, last_seen_at TEXT
    );
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        price_value REAL NOT NULL, currency TEXT DEFAULT 'TRY',
        scraped_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        old_price REAL, new_price REAL, discount_pct REAL,
        active INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        affiliate_url TEXT,
        created_at TEXT NOT NULL, expires_at TEXT
    );
    CREATE TABLE IF NOT EXISTS short_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id INTEGER NOT NULL REFERENCES deals(id),
        slug TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id INTEGER NOT NULL REFERENCES deals(id),
        slug TEXT, ip TEXT, ua TEXT, clicked_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, role TEXT DEFAULT 'user',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tg_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE REFERENCES users(id),
        chat_id TEXT NOT NULL, min_discount_pct INTEGER DEFAULT 10,
        platforms TEXT DEFAULT 'amazon,trendyol,n11',
        active INTEGER DEFAULT 1, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS scan_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER REFERENCES products(id),
        url TEXT NOT NULL, platform TEXT NOT NULL,
        status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 5,
        created_at TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS submit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, ip TEXT, submitted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notify_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE REFERENCES users(id),
        tg_chat_id TEXT DEFAULT '', wa_phone TEXT DEFAULT '',
        min_discount INTEGER DEFAULT 10,
        platforms TEXT DEFAULT 'amazon,trendyol,n11',
        tg_active INTEGER DEFAULT 0, wa_active INTEGER DEFAULT 0,
        updated_at TEXT
    );

    -- ══ YENİ: Çapraz Platform Eşleştirme ════════════════════
    CREATE TABLE IF NOT EXISTS product_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, image_url TEXT, category TEXT,
        created_at TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS product_group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES product_groups(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        match_type TEXT DEFAULT 'manual',
        confidence REAL DEFAULT 1.0,
        added_at TEXT NOT NULL,
        UNIQUE(group_id, product_id)
    );

    -- ══ YENİ: Kullanıcı Yorumları ═══════════════════════════
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        parent_id INTEGER REFERENCES comments(id),
        body TEXT NOT NULL,
        rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        upvotes INTEGER DEFAULT 0, downvotes INTEGER DEFAULT 0,
        is_deleted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS comment_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
        created_at TEXT NOT NULL,
        UNIQUE(comment_id, user_id)
    );

    -- ══ YENİ: SEO İçerik / Blog ═════════════════════════════
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
        summary TEXT, body_html TEXT NOT NULL,
        cover_image TEXT, category TEXT, tags TEXT,
        author_id INTEGER REFERENCES users(id),
        status TEXT DEFAULT 'draft', views INTEGER DEFAULT 0,
        published_at TEXT, created_at TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS article_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER NOT NULL REFERENCES articles(id),
        product_id INTEGER REFERENCES products(id),
        group_id INTEGER REFERENCES product_groups(id),
        sort_order INTEGER DEFAULT 0,
        UNIQUE(article_id, product_id)
    );

    -- ══ YENİ: Güvenlik ══════════════════════════════════════
    CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL, ip TEXT,
        success INTEGER DEFAULT 0, attempted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        token TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_prt_token ON password_reset_tokens(token);

    -- ══ İNDEKSLER ═══════════════════════════════════════════
    CREATE INDEX IF NOT EXISTS idx_products_platform ON products(platform);
    CREATE INDEX IF NOT EXISTS idx_ph_product ON price_history(product_id);
    CREATE INDEX IF NOT EXISTS idx_deals_product ON deals(product_id);
    CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(active, created_at);
    CREATE INDEX IF NOT EXISTS idx_clicks_deal ON clicks(deal_id);
    CREATE INDEX IF NOT EXISTS idx_pgm_group ON product_group_members(group_id);
    CREATE INDEX IF NOT EXISTS idx_pgm_product ON product_group_members(product_id);
    CREATE INDEX IF NOT EXISTS idx_comments_product ON comments(product_id);
    CREATE INDEX IF NOT EXISTS idx_articles_slug ON articles(slug);
    CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
    CREATE INDEX IF NOT EXISTS idx_login_email ON login_attempts(email, attempted_at);

    -- ══ Ürün Takip Listesi ══════════════════════════════════
    CREATE TABLE IF NOT EXISTS product_watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        created_at TEXT NOT NULL,
        UNIQUE(user_id, product_id)
    );
    CREATE INDEX IF NOT EXISTS idx_watchlist_product ON product_watchlist(product_id);
    CREATE INDEX IF NOT EXISTS idx_watchlist_user ON product_watchlist(user_id);
    """)

    # Mevcut tablolara yeni sütun ekle (zaten varsa hata yutulur)
    for col in [("products","brand","TEXT"),("products","category","TEXT"),("products","barcode","TEXT"),("products","stock","TEXT DEFAULT 'unknown'"),
                ("deals","status","TEXT DEFAULT 'pending'"),("deals","affiliate_url","TEXT"),
                ("submit_log","product_id","INTEGER")]:
        try: db.execute(f"ALTER TABLE {col[0]} ADD COLUMN {col[1]} {col[2]}")
        except sqlite3.OperationalError: pass

    _ensure_admin(db)
    db.commit()
    print("[db] Veritabanı hazır (v2).")


def _ensure_admin(db):
    from security import hash_password
    from datetime import datetime
    if not db.execute("SELECT id FROM users WHERE email='admin@firsatvakti.com'").fetchone():
        db.execute("INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                   ("admin","admin@firsatvakti.com",hash_password("admin123"),"admin",
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
