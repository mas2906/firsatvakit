-- Yeni tema (2026-10-13): fırsatlara oy ve anahtar kelimeli fiyat alarmları.

-- Fırsat oyları: +1 iyi fırsat, -1 kötü fırsat. voter = 'u:<user_id>' ya da
-- misafir için 'ip:<sha256(ip) ilk 16>'. Oylar şimdilik gösterilmiyor, ileride
-- FırsatVakti Puanı'na katkı verecek.
CREATE TABLE IF NOT EXISTS deal_votes (
    id         SERIAL PRIMARY KEY,
    deal_id    INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    voter      TEXT NOT NULL,
    vote       SMALLINT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (deal_id, voter)
);
CREATE INDEX IF NOT EXISTS idx_deal_votes_deal ON deal_votes(deal_id);

-- Anahtar kelimeli fiyat alarmı: başlığında keyword geçen ürün en az min_pct
-- indirimle YAYINLANINCA (telegram_pub.publish_deal) kullanıcıya bildirim.
CREATE TABLE IF NOT EXISTS price_alerts (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    keyword          TEXT NOT NULL,
    min_pct          INTEGER NOT NULL DEFAULT 10,
    created_at       TEXT NOT NULL,
    last_notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_alerts_user ON price_alerts(user_id);

-- Bir alarm aynı fırsat için iki kez bildirim göndermesin
CREATE TABLE IF NOT EXISTS price_alert_hits (
    alert_id   INTEGER NOT NULL REFERENCES price_alerts(id) ON DELETE CASCADE,
    deal_id    INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (alert_id, deal_id)
);
