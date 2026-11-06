#!/bin/bash
# Ubuntu'da scraper'i kullanici servisi olarak kurar ve baslatir.
# Windows scraper'i durmadan calistirilmamali: isletim sistemleri arasi kilit yok.
set -e

PROJE=/mnt/d/firsatvakti_new/firsatvakti_new
LOGS=/mnt/d/firsatvakti_new/logs
BIRIM="$HOME/.config/systemd/user"

if ! "$HOME/fv_venv/bin/python" -c "import camoufox, crawlee, curl_cffi" 2>/dev/null; then
    echo "HATA: ~/fv_venv eksik"
    exit 1
fi

mkdir -p "$BIRIM"
cat > "$BIRIM/firsatvakti-scraper.service" <<EOF
[Unit]
Description=FirsatVakti Scraper
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$PROJE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=$HOME/fv_venv/bin/python $PROJE/local_scraper.py
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=20
StandardOutput=truncate:$LOGS/scraper_out.log
StandardError=truncate:$LOGS/scraper_err.log

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
loginctl enable-linger "$(id -un)"
systemctl --user enable firsatvakti-scraper
systemctl --user restart firsatvakti-scraper
sleep 3
echo "linger: $(loginctl show-user "$(id -un)" -p Linger --value)"
echo "servis: $(systemctl --user is-active firsatvakti-scraper)"
echo "otomatik: $(systemctl --user is-enabled firsatvakti-scraper)"
