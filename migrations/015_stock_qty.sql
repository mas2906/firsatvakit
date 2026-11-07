-- Stok adedi (2026-11-07 kullanıcı isteği). Sayfada adet gösteren mağazalar:
-- Hepsiburada (buybox satıcısının adedi), N11 (ürün adedi), Amazon (yalnız "Stokta sadece N
-- adet kaldı" yazısı varsa). Trendyol ana ürünün adedini göstermiyor, NULL kalır.
ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_qty INTEGER;

-- Adet geçmişi: yalnız adet DEĞİŞİNCE satır yazılır. Ardışık düşüşlerden kabaca satış tahmini.
CREATE TABLE IF NOT EXISTS stock_history (
    id          SERIAL PRIMARY KEY,
    product_id  INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_history_product ON stock_history(product_id, id);

-- Trendyol'un bir okuma yolu "Var" yazıyordu, sitede "Stokta" rozeti bu ürünlerde çıkmıyordu.
-- (Not: migration çalıştırıcısı dosyayı noktalı virgülden böler, yorumlarda kullanılmaz.)
UPDATE products SET stock='Stokta Var' WHERE platform='trendyol' AND stock='Var';
