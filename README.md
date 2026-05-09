
```
Türkiye'nin başlıca e-ticaret platformlarından fiyat değişikliklerini otomatik izleyen ve kullanıcılara indirim fırsatlarını bildiren web uygulaması.

**Site:** [firsatvakti.com](https://firsatvakti.com)

---

## Ne Yapıyor?

- Kullanıcılar Amazon, Trendyol, Hepsiburada veya N11'den ürün linki gönderir
- Sistem fiyatı otomatik çeker, onaylanırsa Telegram kanalına ve WhatsApp'a yayınlar
- Arka planda tüm ürünler sürekli taranır — fiyat düşünce izleyicilere bildirim gider
- Aynı ürünü farklı platformlarda bulup fiyat karşılaştırması (çapraz arama) yapar

---

## Teknoloji

| Katman | Araç |
|--------|------|
| Backend | FastAPI + SQLite (WAL) |
| Web Scraping | curl_cffi, Playwright, httpx, BeautifulSoup |
| Bildirim | Telegram Bot API, WhatsApp Business API, E-posta (SMTP) |
| Auth | bcrypt, Google OAuth2, session cookie |
| Sunucu | Uvicorn, VPS (Ubuntu) |

---

## Desteklenen Platformlar

| Platform | Scraper Yöntemi |
|----------|----------------|
| Amazon.com.tr | curl_cffi → Playwright |
| Trendyol.com | curl_cffi → Playwright |
| Hepsiburada.com | curl_cffi → Playwright |
| N11.com | curl_cffi → Playwright |

Her platform için çok katmanlı scraper: önce curl_cffi (Chrome TLS fingerprint) denenır, başarısız olursa Playwright devreye girer.

---

## Özellikler

### Kullanıcılar İçin
- **Fırsat Gönder** — Ürün linki yapıştır, sistem fiyatı çeksin, moderatör onaylasın
- **Watchlist** — Ürünü izlemeye al, fiyat düşünce e-posta / Telegram bildirimi al
- **Çapraz Arama** — Aynı ürünü tüm platformlarda karşılaştır
- **Yorumlar** — Ürünlere yorum, puan, upvote/downvote
- **SEO Blog** — Ürün karşılaştırma makaleleri

### Yöneticiler İçin
- Deal onay / red / düzenleme
- Fiyat geçmişi grafikleri
- Hata takibi (scraper_errors tablosu)
- Telegram admin botu (anlık onay bildirimleri)

---

## Mimari

```
┌─────────────────────────────────────┐
│          VPS (firsatvakti.com)       │
│                                     │
│  FastAPI (main.py)                  │
│    ├── HTTP Routes + Templates      │
│    ├── scheduler.py                 │
│    │     └── Deal expire + DB temizlik │
│    └── scraper_worker.py            │
│          ├── deal_loop  → aktif deal ürünleri sürekli tara
│          └── full_loop  → tüm DB ürünleri sürekli tara
│                                     │
│  SQLite WAL  (/var/www/firsatvakti/)│
└─────────────────────────────────────┘
```

### Scraper Worker (v2)

İki paralel asenkron döngü çalışır:

- **deal_loop** — Aktif deal'lı 17 ürünü öncelikli tarar, bitince hemen yeniden başlar
- **full_loop** — 8000+ ürünü 100'lük batch'lerle tarar; son 10 dakikada tarananları atlar (deal_loop zaten halleder)

Platform başına eşzamanlılık: `amazon=2, trendyol=3, hepsiburada=1, n11=1`

---

## Dosya Yapısı

```
firsatvakti_new/
├── main.py              # FastAPI uygulama, tüm route'lar
├── db.py                # SQLite şema, bağlantı
├── config.py            # Merkezi sabitler (rescan süreleri, limitler)
├── security.py          # Auth, şifre hash, CSRF, rate limit
├── comments.py          # Yorum CRUD
├── cross_search.py      # Çapraz platform arama
├── scraper_worker.py    # Background scanner (deal_loop + full_loop)
├── scheduler.py         # Deal expire + DB temizlik
├── telegram_pub.py      # Telegram bildirimleri
├── whatsapp_pub.py      # WhatsApp Business bildirimleri
├── email_utils.py       # E-posta (şifre sıfırlama, uyarı)
├── affiliate.py         # Affiliate URL yönetimi
├── seo_content.py       # Blog makaleleri, sitemap.xml
├── scrapers/
│   ├── amazon.py
│   ├── trendyol.py
│   ├── hepsiburada.py
│   ├── n11.py
│   ├── router.py        # Platform yönlendirici
│   └── utils.py         # UA havuzu, rate limiter, stream_fetch
└── templates/ + static/ # Jinja2 şablonlar, CSS/JS
```

---

## Ortam Değişkenleri (.env)

```env
DB_PATH=/var/www/firsatvakti/firsatvakti.db

# Telegram
TG_ADMIN_TOKEN=...
TG_ADMIN_CHAT_ID=...
TG_CHANNEL_TOKEN=...
TG_CHANNEL_ID=@firsatvakti1

# WhatsApp Business API
WA_PHONE_NUMBER_ID=...
WA_ACCESS_TOKEN=...

# E-posta
SMTP_HOST=smtp.yandex.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...

# Affiliate Tag'ler
AMAZON_TAG=firsatvakti-21
TRENDYOL_AFF=firsatvakti

# Google OAuth
GOOGLE_CLIENT_ID=...

# Worker
FULL_LOOP_SKIP_MINUTES=10
FRONTEND_WEBHOOK_KEY=firsatvakti-webhook-key
```

---

## Kurulum ve Çalıştırma

```bash
# Bağımlılıklar
pip install -r requirements.txt
playwright install chromium

# Geliştirme
uvicorn main:app --reload --port 8000

# Scraper worker (ayrı terminal)
python scraper_worker.py
```

### VPS'te Deploy

```bash
# Scraper + scheduler dosyalarını deploy et
python deploy_worker.py

# Servisi yönet
systemctl restart firsatvakti
systemctl status firsatvakti

# Worker logları
tail -f /root/firsatvakti/worker.log
```

---

## Veritabanı Tabloları

| Tablo | İçerik |
|-------|--------|
| products | Ürün bilgileri, son fiyat, stok durumu |
| price_history | Tüm fiyat değişimleri |
| deals | Onaylı fırsatlar (active=1 olanlar yayında) |
| users | Kullanıcılar, auth bilgileri |
| product_watchlist | Kullanıcı izleme listesi |
| comments / comment_votes | Yorumlar ve oylar |
| articles | SEO blog makaleleri |
| product_groups | Çapraz arama grupları |
| scraper_errors | Scraper hata kayıtları |

---

## Güvenlik

- bcrypt şifre hash + eski sha256 hesapları için migration
- Brute-force koruması (5 hatalı giriş → 15 dk kilitleme)
- CSRF token (cookie tabanlı)
- IP bazlı rate limiting (submit: 5/dk, login: 10/dk)
- Kullanıcı limitleri (misafir: 3/gün, üye: 20/gün submit)