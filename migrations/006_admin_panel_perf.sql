-- Migration 006: Admin panel sorgu performans iyileştirmeleri

-- deals: status + created_at composite — admin pending deals sorgusu + ORDER BY
CREATE INDEX IF NOT EXISTS idx_deals_status_created ON deals(status, created_at DESC);

-- products: platform + stock composite — "amazon stok yok" filtresi
CREATE INDEX IF NOT EXISTS idx_products_platform_stock ON products(platform, stock);

-- clicks: deal bazlı delete için (zaten var, eksikse ekle)
CREATE INDEX IF NOT EXISTS idx_clicks_deal_id ON clicks(deal_id);

-- short_links: deal bazlı delete için (zaten var, eksikse ekle)
CREATE INDEX IF NOT EXISTS idx_short_links_deal_id ON short_links(deal_id);
