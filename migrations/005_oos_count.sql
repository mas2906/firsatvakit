-- Migration 005: Üst üste stok yok sayacı
-- 3 kez üst üste stok yok dönerse ürün otomatik silinir
ALTER TABLE products ADD COLUMN IF NOT EXISTS oos_count INTEGER DEFAULT 0;
