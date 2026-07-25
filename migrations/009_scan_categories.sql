-- Kategori tabanlı ürün keşfi: admin panelinden keyword/kategori girilir,
-- scraper otomatik arar ve yeni ürün URL'lerini scan_queue'ya ekler.
CREATE TABLE IF NOT EXISTS scan_categories (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    platform   TEXT DEFAULT NULL,
    page_limit INTEGER DEFAULT 2,
    enabled    INTEGER DEFAULT 1,
    last_scanned_at TEXT,
    created_at TEXT DEFAULT TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_scan_categories_enabled ON scan_categories(enabled, last_scanned_at);
