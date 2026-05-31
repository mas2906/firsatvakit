ALTER TABLE deals ADD COLUMN IF NOT EXISTS coupon TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS variants TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS oos_count INTEGER DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_deals_status_created ON deals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_products_platform_stock ON products(platform, stock);
CREATE INDEX IF NOT EXISTS idx_clicks_deal_id ON clicks(deal_id);
CREATE INDEX IF NOT EXISTS idx_short_links_deal_id ON short_links(deal_id);
