#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Veritabanı — PostgreSQL (psycopg2)."""

import os
import re
import logging
import threading
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    raise ImportError("psycopg2 yüklü değil: pip install psycopg2-binary")

log = logging.getLogger("db")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://firsatvakti:firsatvakti@localhost/firsatvakti"
)

_local = threading.local()

# ── SQL dönüştürücü ──────────────────────────────────────────────────────────

_DATE_FUNC = re.compile(r"DATE\((?!'now')([^)]+)\)", re.IGNORECASE)

_DT_INTERVAL = re.compile(
    r"datetime\('now'\s*,\s*'([+-])(\d+)\s+(second|minute|hour|day|month|year)s?'\)",
    re.IGNORECASE,
)
_DT_NOW = re.compile(r"datetime\('now'\)", re.IGNORECASE)
_DATE_INTERVAL = re.compile(
    r"date\('now'\s*,\s*'([+-])(\d+)\s+(second|minute|hour|day|month|year)s?'\)",
    re.IGNORECASE,
)
_DATE_NOW = re.compile(r"date\('now'\)", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
_PARAM = re.compile(r"\?")

# GROUP_CONCAT(expr, sep) → STRING_AGG(expr, sep)
# GROUP_CONCAT(expr)      → STRING_AGG(expr, ',')
_GROUP_CONCAT = re.compile(r"\bGROUP_CONCAT\s*\(", re.IGNORECASE)

_TS_FMT = "YYYY-MM-DD HH24:MI:SS"
_D_FMT = "YYYY-MM-DD"


def _replace_group_concat(sql: str) -> str:
    """GROUP_CONCAT(expr) → STRING_AGG(expr, ',')
       GROUP_CONCAT(expr, sep) → STRING_AGG(expr, sep)"""
    result = []
    i = 0
    upper = sql.upper()
    while i < len(sql):
        pos = upper.find("GROUP_CONCAT(", i)
        if pos == -1:
            result.append(sql[i:])
            break
        result.append(sql[i:pos])
        # pos = start of GROUP_CONCAT, find the opening paren
        start = pos + len("GROUP_CONCAT(")
        depth = 1
        j = start
        while j < len(sql) and depth > 0:
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
            j += 1
        inner = sql[start:j - 1]  # contents between outer parens
        # Split into args at top-level commas
        args, cur_arg, d = [], [], 0
        for ch in inner:
            if ch == "(":
                d += 1
            elif ch == ")":
                d -= 1
            if ch == "," and d == 0:
                args.append("".join(cur_arg).strip())
                cur_arg = []
            else:
                cur_arg.append(ch)
        if cur_arg:
            args.append("".join(cur_arg).strip())
        if len(args) == 1:
            result.append(f"STRING_AGG({args[0]}, ',')")
        else:
            result.append(f"STRING_AGG({', '.join(args)})")
        i = j
        upper = sql.upper()  # recalculate after continuing
    return "".join(result)


def _convert(sql: str) -> str:
    """SQLite SQL sözdizimini PostgreSQL'e çevir."""
    # GROUP_CONCAT → STRING_AGG
    sql = _replace_group_concat(sql)

    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    has_ioi = bool(_INSERT_OR_IGNORE.search(sql))
    if has_ioi:
        sql = _INSERT_OR_IGNORE.sub("INSERT", sql)

    # datetime('now', '-N unit') → TO_CHAR(NOW() ± INTERVAL 'N unit', '...')
    def _dt_iv(m):
        sign, n, unit = m.group(1), m.group(2), m.group(3).lower()
        op = "-" if sign == "-" else "+"
        return f"TO_CHAR(NOW() {op} INTERVAL '{n} {unit}s', '{_TS_FMT}')"

    sql = _DT_INTERVAL.sub(_dt_iv, sql)
    sql = _DT_NOW.sub(f"TO_CHAR(NOW(), '{_TS_FMT}')", sql)

    def _date_iv(m):
        sign, n, unit = m.group(1), m.group(2), m.group(3).lower()
        op = "-" if sign == "-" else "+"
        return f"TO_CHAR(CURRENT_DATE {op} INTERVAL '{n} {unit}s', '{_D_FMT}')"

    sql = _DATE_INTERVAL.sub(_date_iv, sql)
    sql = _DATE_NOW.sub(f"TO_CHAR(CURRENT_DATE, '{_D_FMT}')", sql)

    # DATE(col) → LEFT(col, 10)  — TEXT sütunlardaki tarih önekini al
    sql = _DATE_FUNC.sub(lambda m: f"LEFT({m.group(1)}, 10)", sql)

    # LIKE '%...' ifadelerindeki % karakterlerini psycopg2 için kaçır,
    # ardından ? parametrelerini %s ile değiştir.
    sql = sql.replace("%", "%%")
    sql = _PARAM.sub("%s", sql)

    if has_ioi:
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING RETURNING *"

    return sql


# ── Row sarmalayıcı ──────────────────────────────────────────────────────────

class _Row:
    """Hem sütun adı (row["col"]) hem tam sayı indeksi (row[0]) destekler."""

    __slots__ = ("_d", "_vals")

    def __init__(self, d):
        object.__setattr__(self, "_d", dict(d))
        object.__setattr__(self, "_vals", tuple(dict(d).values()))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._d[key]

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def __repr__(self):
        return repr(self._d)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()


class _Cursor:
    """sqlite3 Cursor uyumlu — psycopg2 sonuçlarını sarmalar."""

    __slots__ = ("rowcount", "lastrowid", "_rows", "_idx")

    def __init__(self, rows=None, last_id=None, rowcount=0):
        self._rows = rows or []
        self._idx = 0
        self.rowcount = rowcount
        self.lastrowid = last_id

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return _Row(row)

    def fetchall(self):
        result = [_Row(r) for r in self._rows[self._idx:]]
        self._idx = len(self._rows)
        return result

    def __iter__(self):
        while self._idx < len(self._rows):
            yield _Row(self._rows[self._idx])
            self._idx += 1


# ── Bağlantı sarmalayıcı ─────────────────────────────────────────────────────

class _Connection:
    """sqlite3 Connection uyumlu — psycopg2 bağlantısını sarmalar."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=None) -> _Cursor:
        sql_pg = _convert(sql)
        is_insert = bool(re.match(r"\s*INSERT", sql_pg, re.IGNORECASE))
        needs_ret = (
                is_insert
                and "RETURNING" not in sql_pg.upper()
                and "ON CONFLICT" not in sql_pg.upper()
                # ON CONFLICT DO NOTHING RETURNING * zaten _convert tarafından eklendi
        )

        if needs_ret:
            sql_pg = sql_pg.rstrip().rstrip(";") + " RETURNING id"

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql_pg, params or ())
        except Exception:
            self._conn.rollback()
            raise

        rowcount = cur.rowcount
        last_id = None
        rows: list = []

        if cur.description:
            raw = cur.fetchall()
            if is_insert:
                if raw:
                    last_id = raw[0].get("id")  # RETURNING * → id varsa al, yoksa None
            else:
                rows = list(raw)

        return _Cursor(rows=rows, last_id=last_id, rowcount=rowcount)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


# ── Genel API ────────────────────────────────────────────────────────────────

def get_db() -> _Connection:
    conn = getattr(_local, "pg_conn", None)
    if conn is None or conn._conn.closed:
        raw = psycopg2.connect(DATABASE_URL)
        raw.autocommit = False
        # Admin silme işlemleri lock beklemede takılmasın
        with raw.cursor() as _c:
            _c.execute("SET lock_timeout = '3s'")
            _c.execute("SET statement_timeout = '15s'")
        conn = _Connection(raw)
        _local.pg_conn = conn
        log.info(f"[db] PostgreSQL bağlantısı açıldı ({DATABASE_URL[:40]}...)")
    else:
        # Önceki request'ten kalan uncommitted transaction'ı temizle
        try:
            if conn._conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
                conn._conn.rollback()
        except Exception:
            pass
    return conn


def commit_with_retry(db: _Connection, **_):
    db.commit()


def init_db():
    db = get_db()
    _create_schema(db)
    _run_migrations(db)
    _ensure_admin(db)
    db.commit()
    print("[db] Veritabanı hazır (PostgreSQL).")


def _run_migrations(db: _Connection):
    """migrations/ klasöründeki .sql dosyalarını sırayla çalıştırır."""
    import glob, os as _os
    migrations_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "migrations")
    if not _os.path.isdir(migrations_dir):
        return

    # Migration takip tablosu
    cur = db._conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    db._conn.commit()

    cur.execute("SELECT filename FROM _migrations")
    applied = {r[0] for r in cur.fetchall()}

    sql_files = sorted(glob.glob(_os.path.join(migrations_dir, "*.sql")))
    for path in sql_files:
        fname = _os.path.basename(path)
        if fname in applied:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                # Strip leading comment-only lines so statements that begin with
                # a comment block (e.g. "-- note\nALTER TABLE ...") are not skipped.
                non_comment = "\n".join(
                    line for line in stmt.splitlines()
                    if line.strip() and not line.strip().startswith("--")
                ).strip()
                if not non_comment:
                    continue
                try:
                    cur.execute(non_comment)
                except Exception as e:
                    log.warning(f"[migration] {fname} stmt atlandı: {e}")
            cur.execute(
                "INSERT INTO _migrations(filename, applied_at) VALUES(%s, NOW())",
                (fname,)
            )
            db._conn.commit()
            log.info(f"[migration] ✔ {fname} uygulandı")
        except Exception as e:
            log.error(f"[migration] {fname} HATA: {e}")
            db._conn.rollback()

    cur.close()


def _create_schema(db: _Connection):
    stmts = [
        # ── Ana tablolar ──────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            platform TEXT NOT NULL, asin_or_id TEXT,
            source_url TEXT UNIQUE NOT NULL,
            title TEXT, image_url TEXT, description TEXT,
            rating REAL, review_count INTEGER,
            brand TEXT, category TEXT, barcode TEXT,
            stock TEXT DEFAULT 'unknown',
            ai_review_summary TEXT,
            oos_count INTEGER DEFAULT 0,
            variants TEXT,
            first_seen_at TEXT, last_seen_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS price_history (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            price_value REAL NOT NULL, currency TEXT DEFAULT 'TRY',
            scraped_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS deals (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            old_price REAL, new_price REAL, discount_pct REAL,
            active INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            affiliate_url TEXT,
            cart_discount INTEGER DEFAULT 0,
            admin_note TEXT,
            created_at TEXT NOT NULL, expires_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS short_links (
            id SERIAL PRIMARY KEY,
            deal_id INTEGER NOT NULL REFERENCES deals(id),
            slug TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS clicks (
            id SERIAL PRIMARY KEY,
            deal_id INTEGER NOT NULL REFERENCES deals(id),
            slug TEXT, ip TEXT, ua TEXT, clicked_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT DEFAULT 'user',
            google_id TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS tg_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id),
            chat_id TEXT NOT NULL, min_discount_pct INTEGER DEFAULT 10,
            platforms TEXT DEFAULT 'amazon,trendyol,n11',
            active INTEGER DEFAULT 1, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS scan_queue (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            url TEXT NOT NULL, platform TEXT NOT NULL,
            status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 5,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS submit_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER, ip TEXT,
            product_id INTEGER,
            submitted_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS notify_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id),
            tg_chat_id TEXT DEFAULT '', wa_phone TEXT DEFAULT '',
            min_discount INTEGER DEFAULT 10,
            platforms TEXT DEFAULT 'amazon,trendyol,n11',
            tg_active INTEGER DEFAULT 0, wa_active INTEGER DEFAULT 0,
            updated_at TEXT
        )""",
        # ── Çapraz platform ───────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS product_groups (
            id SERIAL PRIMARY KEY,
            name TEXT, image_url TEXT, category TEXT,
            created_at TEXT NOT NULL, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS product_group_members (
            id SERIAL PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES product_groups(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            match_type TEXT DEFAULT 'manual',
            confidence REAL DEFAULT 1.0,
            added_at TEXT NOT NULL,
            UNIQUE(group_id, product_id)
        )""",
        # ── Yorumlar ─────────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            parent_id INTEGER REFERENCES comments(id),
            body TEXT NOT NULL,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            upvotes INTEGER DEFAULT 0, downvotes INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS comment_votes (
            id SERIAL PRIMARY KEY,
            comment_id INTEGER NOT NULL REFERENCES comments(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
            created_at TEXT NOT NULL,
            UNIQUE(comment_id, user_id)
        )""",
        # ── SEO / Blog ────────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
            summary TEXT, body_html TEXT NOT NULL,
            cover_image TEXT, category TEXT, tags TEXT,
            author_id INTEGER REFERENCES users(id),
            status TEXT DEFAULT 'draft', views INTEGER DEFAULT 0,
            published_at TEXT, created_at TEXT NOT NULL, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS article_products (
            id SERIAL PRIMARY KEY,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            product_id INTEGER REFERENCES products(id),
            group_id INTEGER REFERENCES product_groups(id),
            sort_order INTEGER DEFAULT 0,
            UNIQUE(article_id, product_id)
        )""",
        # ── Güvenlik ─────────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL, ip TEXT,
            success INTEGER DEFAULT 0, attempted_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        # ── Takip / Hata ─────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS product_watchlist (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            created_at TEXT NOT NULL,
            UNIQUE(user_id, product_id)
        )""",
        """CREATE TABLE IF NOT EXISTS scraper_errors (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            platform TEXT NOT NULL,
            url TEXT,
            error_msg TEXT,
            occurred_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS link_queue (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL,
            platform TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id),
            ip TEXT,
            status TEXT DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            reviewed_at TEXT
        )""",
        # Aynı ürün + aynı indirim oranı 24 saat içinde tekrar yayınlanmasın
        """CREATE TABLE IF NOT EXISTS deal_publish_log (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            discount_pct INTEGER NOT NULL,
            published_at TEXT NOT NULL,
            deal_id INTEGER REFERENCES deals(id)
        )""",
        # ── Amazon satıcı teklifleri ──────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS seller_offers (
            id SERIAL PRIMARY KEY,
            asin TEXT NOT NULL,
            price TEXT,
            condition TEXT,
            seller TEXT,
            seller_rating TEXT,
            rating_count TEXT,
            positive_pct TEXT,
            ships_from TEXT,
            scraped_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_so_asin ON seller_offers(asin, scraped_at)",
        # ── İndeksler ────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_products_platform ON products(platform)",
        "CREATE INDEX IF NOT EXISTS idx_ph_product ON price_history(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_deals_product ON deals(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(active, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_clicks_deal ON clicks(deal_id)",
        "CREATE INDEX IF NOT EXISTS idx_pgm_group ON product_group_members(group_id)",
        "CREATE INDEX IF NOT EXISTS idx_pgm_product ON product_group_members(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_comments_product ON comments(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_articles_slug ON articles(slug)",
        "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status)",
        "CREATE INDEX IF NOT EXISTS idx_login_email ON login_attempts(email, attempted_at)",
        "CREATE INDEX IF NOT EXISTS idx_sq_status_priority ON scan_queue(status, priority, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sq_updated ON scan_queue(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_watchlist_product ON product_watchlist(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_watchlist_user ON product_watchlist(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_se_product ON scraper_errors(product_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_lq_status ON link_queue(status, submitted_at)",
        "CREATE INDEX IF NOT EXISTS idx_prt_token ON password_reset_tokens(token)",
        "CREATE INDEX IF NOT EXISTS idx_dpl_lookup ON deal_publish_log(product_id, discount_pct, published_at)",
    ]
    cur = db._conn.cursor()
    for stmt in stmts:
        cur.execute(stmt)
    cur.close()


def _ensure_admin(db: _Connection):
    import secrets as _secrets
    from security import hash_password
    from datetime import datetime
    exists = db.execute(
        "SELECT id FROM users WHERE email='admin@firsatvakti.com'"
    ).fetchone()
    if not exists:
        first_pw = _secrets.token_urlsafe(14)
        db.execute(
            "INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
            ("admin", "admin@firsatvakti.com", hash_password(first_pw), "admin",
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        print(f"[db] ✅ Admin kullanıcısı oluşturuldu.")
        print(f"[db] 🔑 İlk şifre: {first_pw}  ← admin panelinden hemen değiştirin!")
