-- Silinen ürün kaydı (2026-10-31): _delete_product_cascade ürünü tüm geçmişiyle
-- siliyor ve iz bırakmıyordu. Admin panelindeki "Silinen (24s)" kartı ve neden
-- dağılımı (stok yok, kaldırıldı, ölü link, manuel...) bu tablodan gelir.
CREATE TABLE IF NOT EXISTS deleted_products (
    id          SERIAL PRIMARY KEY,
    product_id  INTEGER NOT NULL,
    platform    TEXT,
    title       TEXT,
    source_url  TEXT,
    reason      TEXT NOT NULL,
    deleted_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deleted_products_at ON deleted_products(deleted_at);
