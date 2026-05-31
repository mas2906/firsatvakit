-- Migration 004: Performans index'leri
-- Sık kullanılan WHERE koşulları için B-tree index'leri

-- Ana sayfa sorgusu: WHERE d.active=1
CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(active);

-- Ürün bazlı aktif deal sorgusu (deal detay, watchlist)
CREATE INDEX IF NOT EXISTS idx_deals_product_active ON deals(product_id, active);

-- Deal durumu sorguları (admin panel)
CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);

-- Deal oluşturulma tarihi (sıralama)
CREATE INDEX IF NOT EXISTS idx_deals_created_at ON deals(created_at DESC);

-- scan_queue: scraper polling
CREATE INDEX IF NOT EXISTS idx_scan_queue_status_priority ON scan_queue(status, priority ASC, created_at ASC);

-- price_history: ürün bazlı fiyat geçmişi sorgusu
CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, scraped_at DESC);

-- product_watchlist: kullanıcı bazlı izleme listesi
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON product_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_product ON product_watchlist(product_id);

-- clicks: deal bazlı tıklama sayısı (COUNT subquery)
CREATE INDEX IF NOT EXISTS idx_clicks_deal ON clicks(deal_id);

-- short_links: slug lookup (kısa link yönlendirme)
CREATE INDEX IF NOT EXISTS idx_short_links_slug ON short_links(slug);
CREATE INDEX IF NOT EXISTS idx_short_links_deal ON short_links(deal_id);

-- product_group_members: karşılaştırma sorguları
CREATE INDEX IF NOT EXISTS idx_pgm_product ON product_group_members(product_id);
CREATE INDEX IF NOT EXISTS idx_pgm_group ON product_group_members(group_id);

-- link_queue: admin onay kuyruğu
CREATE INDEX IF NOT EXISTS idx_link_queue_status ON link_queue(status);

-- scraper_errors: son 24 saat sorgusu
CREATE INDEX IF NOT EXISTS idx_scraper_errors_occurred ON scraper_errors(occurred_at DESC);
