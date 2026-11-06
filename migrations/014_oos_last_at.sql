-- Amazon'da iki "stok yok" sayımı arasındaki süreyi ölçmek için son sayım zamanı — 2026-11-07
ALTER TABLE products ADD COLUMN IF NOT EXISTS oos_last_at TEXT;
