-- Migration 001: Temel tablo şemasını doğrula, eksikleri ekle
-- Bu migration idempotent'tir — tekrar çalıştırmak güvenlidir

-- scan_queue retry_count kolonu
ALTER TABLE scan_queue ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;

-- products barcode kolonu
ALTER TABLE products ADD COLUMN IF NOT EXISTS barcode TEXT;

-- products brand kolonu
ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT;

-- products seller kolonu
ALTER TABLE products ADD COLUMN IF NOT EXISTS seller TEXT;

-- products review_count kolonu
ALTER TABLE products ADD COLUMN IF NOT EXISTS review_count INTEGER;

-- settings tablosu unique constraint
CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_key ON settings(key);

-- scan_queue updated_at kolonu
ALTER TABLE scan_queue ADD COLUMN IF NOT EXISTS updated_at TEXT;

-- deals cart_discount kolonu
ALTER TABLE deals ADD COLUMN IF NOT EXISTS cart_discount INTEGER DEFAULT 0;
