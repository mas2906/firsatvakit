-- Ürünün son taramadaki buybox satıcısı (Trendyol/Hepsiburada/N11/Amazon) — 2026-11-02
ALTER TABLE products ADD COLUMN IF NOT EXISTS seller TEXT;
