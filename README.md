# 🔥 FırsatVakti v2 — Yenilikler

## v1 → v2 Değişiklik Özeti

### Yeni Özellikler
- ⚖️ **Çapraz Platform Fiyat Karşılaştırma** — Ürünü otomatik diğer platformlarda arar
- 💬 **Kullanıcı Yorum Sistemi** — Puan (1-5), yanıt, oylama, spam koruması
- 📝 **SEO Blog** — Makale/rehber/karşılaştırma yazıları, admin editörü
- 🔒 **bcrypt Güvenlik** — Eski SHA256 otomatik migrate, rate limiting
- 🗺 **Sitemap & Robots.txt** — SEO altyapısı

### Yeni Dosyalar
- `security.py` — bcrypt, rate limiting, şifre migrasyonu
- `cross_search.py` — 4 platformda arama, ürün gruplama, fiyat karşılaştırma
- `comments.py` — Yorum CRUD, oylama, thread yanıtları
- `seo_content.py` — Makale CRUD, meta tag, sitemap, Türkçe slug

### Değişen Dosyalar
- `main.py` — Tüm yeni route'lar entegre
- `db.py` — 7 yeni tablo, 3 yeni sütun, indeksler
- `requirements.txt` — bcrypt eklendi

### Yeni Template'ler
- `compare.html` — Fiyat karşılaştırma sayfası
- `blog_list.html` — Blog listesi
- `blog_detail.html` — Makale detay
- `admin_articles.html` — Admin makale yönetimi
- `admin_article_edit.html` — Makale editörü

### Güncellenen Template'ler
- `deal.html` — Karşılaştırma kutusu + yorum sistemi eklendi
- `submit.html` — Çapraz platform arama checkbox'ları eklendi

### Yeni URL'ler
- `/compare/{group_id}` — Fiyat karşılaştırma
- `/blog` ve `/blog/{slug}` — Blog
- `/admin/articles` — Makale yönetimi
- `/api/comment` — Yorum API
- `/sitemap.xml` ve `/robots.txt`

## Kurulum
```bash
pip install -r requirements.txt   # bcrypt eklendi
# Yeni dosyaları kopyala, restart et — DB otomatik migrate olur
systemctl restart firsatvakti
```
