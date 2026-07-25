# FırsatVakti — Lokal Kurulum Rehberi

## Ön Koşullar (Zaten Hazır ✅)

- Python 3.12 + `.venv` kurulu
- camoufox, playwright, curl_cffi, fastapi, uvicorn — hepsi yüklü
- `.env` dosyası mevcut

---

## Adım 1 — Veritabanı Bağlantısı

İki seçenek var:

### Seçenek A: VPS'teki PostgreSQL'e SSH Tüneli (Önerilen — Kurulum Yok)

VPS'te çalışan PostgreSQL'i yerel olarak kullanmak için bir SSH tüneli açın.

**Yeni bir terminal/PowerShell açın ve çalıştırın:**
```powershell
ssh -L 5432:localhost:5432 KULLANICI@VPS_IP_ADRESI -N
```
> Bu terminal açık kaldığı sürece tünel aktif. Kapatmayın.

`.env` dosyasındaki `DATABASE_URL`'i **değiştirmeyin** (`localhost:5432` zaten doğru).

---

### Seçenek B: Yerel PostgreSQL Kurulumu

**1. İndirin ve kurun:**
- https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
- Versiyon 16 seçin, Windows x86-64
- Kurulum sırasında `postgres` kullanıcısı için bir şifre belirleyin
- Port: 5432 (default)

**2. Kurulum scripti çalıştırın (PostgreSQL kurulduktan sonra):**
```powershell
.\setup_db.ps1
```
> Bu script kullanıcı + database oluşturur, `.env`'deki `DATABASE_URL`'i günceller ve schema'yı çalıştırır.

---

## Adım 2 — Uygulamayı Başlatın

**Terminal 1 — FastAPI sunucu:**
```
start_app.bat
```
veya:
```powershell
.\.venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000 --reload
```

**Terminale şunu görmelisiniz:**
```
[db] Admin kullanıcısı oluşturuldu.
[db] İlk şifre: xxxxxxxxxxxxxxxx  ← bunu kopyalayın!
[db] Veritabanı hazır (PostgreSQL).
INFO: Application startup complete.
```

**Tarayıcıda açın:** http://localhost:8000

---

## Adım 3 — Admin Paneline Giriş

- URL: http://localhost:8000/admin
- E-posta: `admin@firsatvakti.com`
- Şifre: Terminalde görünen `İlk şifre:` değeri

---

## Adım 4 — İlk Kategori Ekleyin

Admin panelinde **Kategoriler** → **Yeni Kategori Ekle**:
- **İsim:** Örn. `Oyun Kulaklıkları`
- **Keyword:** Örn. `oyun kulaklığı`
- **Platform:** `trendyol`
- **Sayfa Limiti:** `2`

---

## Adım 5 — Kategori Scraper'ı Başlatın

**Terminal 2 (yeni):**
```
start_scraper.bat
```
veya:
```powershell
.\.venv\Scripts\python.exe local_scraper.py
```

**15 saniye sonra ilk tarama başlar.** Terminalde şunu göreceksiniz:
```
[category] Kategori keşif worker başladı
[category] 1 kategori taranacak
[category] #1 'Oyun Kulaklıkları' taranıyor (platform=trendyol...)
[category] #1 'oyun kulaklığı' → 47 benzersiz ürün kart
[category] #1 'oyun kulaklığı' → 23 yeni, 0 güncellendi, 5 deal
```

---

## Adım 6 — Deal Onaylayın

Admin paneli → **Bekleyen Fırsatlar** → Deal'ı inceleyin → **Onayla** veya **Reddet**

Onaylanan deal otomatik olarak:
- Ana sayfada yayınlanır
- Telegram kanalına gönderilir (TG_CHANNEL_TOKEN ayarlıysa)

---

## Özet Akış

```
Terminal 1: start_app.bat           → http://localhost:8000
Terminal 2: start_scraper.bat       → 10dk'da bir kategori tarar
Tarayıcı:  /admin                   → Deal onay/red
```

---

## Sık Karşılaşılan Sorunlar

| Sorun | Çözüm |
|-------|-------|
| `Connection refused port 5432` | SSH tüneli açık mı? Veya setup_db.ps1 çalıştırdınız mı? |
| `Admin şifresi nerede?` | İlk `start_app.bat` çalıştırıldığında terminale yazdırılır |
| `Camoufox hata verdi` | `.venv\Scripts\camoufox.exe fetch` çalıştırın |
| `Telegram bildirimi gelmiyor` | `.env`'deki TG_ADMIN_TOKEN ve TG_ADMIN_CHAT_ID'yi kontrol edin |
| `Deal oluşmuyor` | `MIN_DISCOUNT_PCT` değerini düşürün (default: 5) |
