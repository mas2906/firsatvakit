-- Migration 002: 24 saatlik indirim dedup logu
-- Aynı ürün + aynı indirim oranı 24 saat içinde tekrar yayınlanmasın

CREATE TABLE IF NOT EXISTS deal_publish_log (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    discount_pct INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    deal_id INTEGER REFERENCES deals(id)
);

CREATE INDEX IF NOT EXISTS idx_dpl_lookup ON deal_publish_log(product_id, discount_pct, published_at);
