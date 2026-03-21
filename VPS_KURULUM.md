# 🚀 FırsatVakti — VPS Kurulum Kılavuzu (Ubuntu 22.04)

---

## 1. VPS'e Bağlan & Sistem Hazırlığı

```bash
ssh root@SUNUCU_IP

# Sistem güncelle
apt update && apt upgrade -y

# Gerekli paketler
apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx git ufw
```

---

## 2. Güvenlik Duvarı (UFW)

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw enable
ufw status
```

---

## 3. Proje Dosyalarını Yükle

### A) GitHub üzerinden (önerilir)
```bash
mkdir -p /var/www/firsatvakti
cd /var/www/firsatvakti
git clone https://github.com/KULLANICI/firsatvakti.git .
```

### B) Elle yükleme (SCP/SFTP)
```bash
# Yerel bilgisayardan:
scp -r ./firsatvakti root@SUNUCU_IP:/var/www/firsatvakti
```

---

## 4. Python Sanal Ortam & Bağımlılıklar

```bash
cd /var/www/firsatvakti

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5. .env Dosyasını Oluştur

```bash
cp .env.example .env
nano .env
```

**`.env` içeriği:**
```env
SECRET_KEY=BURAYA_GUCLU_RASTGELE_SIFRE  # python3 -c "import secrets; print(secrets.token_hex(32))"
DB_PATH=/var/www/firsatvakti/firsatvakti.db

# Telegram
TG_CHANNEL_TOKEN=1234567890:ABCdef...
TG_CHANNEL_ID=@firsatvakti

# Affiliate
AMAZON_TAG=firsatvakti-21
TRENDYOL_AFF=firsatvakti
N11_AFF=firsatvakti
HB_AFF=firsatvakti
```

**Güçlü secret key üretmek için:**
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 6. Dosya İzinleri

```bash
useradd -r -s /bin/false firsatvakti
chown -R firsatvakti:firsatvakti /var/www/firsatvakti
chmod 750 /var/www/firsatvakti
chmod 600 /var/www/firsatvakti/.env
```

---

## 7. Systemd Servisleri

### 7a. Ana Web Uygulaması

```bash
nano /etc/systemd/system/firsatvakti.service
```

```ini
[Unit]
Description=FirsatVakti Web App
After=network.target

[Service]
User=firsatvakti
Group=firsatvakti
WorkingDirectory=/var/www/firsatvakti
EnvironmentFile=/var/www/firsatvakti/.env
ExecStart=/var/www/firsatvakti/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 7b. Zamanlayıcı (Arka plan tarama)

```bash
nano /etc/systemd/system/firsatvakti-scheduler.service
```

```ini
[Unit]
Description=FirsatVakti Scheduler
After=network.target firsatvakti.service

[Service]
User=firsatvakti
Group=firsatvakti
WorkingDirectory=/var/www/firsatvakti
EnvironmentFile=/var/www/firsatvakti/.env
ExecStart=/var/www/firsatvakti/venv/bin/python3 scheduler.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Servisleri Başlat

```bash
systemctl daemon-reload

systemctl enable firsatvakti firsatvakti-scheduler
systemctl start  firsatvakti firsatvakti-scheduler

# Durumu kontrol et
systemctl status firsatvakti
systemctl status firsatvakti-scheduler

# Log takip
journalctl -u firsatvakti -f
```

---

## 8. Nginx Yapılandırması

```bash
nano /etc/nginx/sites-available/firsatvakti
```

```nginx
server {
    listen 80;
    server_name firsatvakti.com www.firsatvakti.com;

    # Static dosyalar doğrudan Nginx'ten sun
    location /static/ {
        alias /var/www/firsatvakti/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # FastAPI'ye yönlendir
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120;
    }

    # Güvenlik başlıkları
    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";
}
```

```bash
# Etkinleştir ve test et
ln -s /etc/nginx/sites-available/firsatvakti /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

---

## 9. SSL Sertifikası (Ücretsiz — Let's Encrypt)

> ⚠ Domain DNS'ini sunucuya yönlendirmiş olmalısın (A kaydı).

```bash
certbot --nginx -d firsatvakti.com -d www.firsatvakti.com

# E-posta gir, şartları kabul et
# Certbot Nginx'i otomatik SSL için günceller

# Sertifika yenileme testini doğrula
certbot renew --dry-run
```

---

## 10. İlk Admin Girişi

Tarayıcıda aç: `https://firsatvakti.com/login`

| Alan | Değer |
|------|-------|
| E-posta | `admin@firsatvakti.com` |
| Şifre | `admin123` |

> ⚠ İlk girişten hemen sonra şifreyi değiştirin!

Admin paneli: `https://firsatvakti.com/admin`

---

## 11. Güncelleme (Deploy Sonrası)

```bash
cd /var/www/firsatvakti

# GitHub'dan güncelle
git pull

# Bağımlılık değişikliği varsa
source venv/bin/activate
pip install -r requirements.txt

# Servisleri yeniden başlat
systemctl restart firsatvakti firsatvakti-scheduler
```

---

## 12. Faydalı Komutlar

```bash
# Logları izle (canlı)
journalctl -u firsatvakti -f
journalctl -u firsatvakti-scheduler -f

# Servis durumu
systemctl status firsatvakti

# Nginx log
tail -f /var/log/nginx/error.log
tail -f /var/log/nginx/access.log

# DB'yi doğrudan incele
sqlite3 /var/www/firsatvakti/firsatvakti.db
.tables
SELECT COUNT(*) FROM deals WHERE active=1;
.quit

# Port dinleme kontrolü
ss -tlnp | grep 8000
```

---

## 13. Önerilen VPS Seçenekleri

| Sağlayıcı | Plan | Fiyat | Not |
|-----------|------|-------|-----|
| Hetzner | CX22 | ~4€/ay | En iyi fiyat/performans |
| DigitalOcean | Basic Droplet 1GB | ~6$/ay | Kolay panel |
| Contabo | VPS S | ~5€/ay | Büyük disk |
| Linode | Nanode 1GB | ~5$/ay | Güvenilir |

**Minimum gereksinim:** 1 vCPU, 1 GB RAM, Ubuntu 22.04

