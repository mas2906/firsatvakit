# ── FırsatVakti — PostgreSQL Kurulum Scripti (Windows) ──────────────
# Çalıştırmadan önce PostgreSQL'i kur:
# https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
# Kurulum sırasında postgres kullanıcısı için bir şifre belirleyin.
#
# Kullanım: .\setup_db.ps1

$PG_USER     = "firsatvakti"
$PG_PASS     = "firsatvakti123"   # İstediğinizle değiştirin
$PG_DB       = "firsatvakti"
$PG_HOST     = "localhost"
$PG_PORT     = "5432"

# psql'in PATH'te olduğunu kontrol et
$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
    # EDB varsayılan kurulum yolu
    $psql = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
    if (-not (Test-Path $psql)) {
        $psql = "C:\Program Files\PostgreSQL\15\bin\psql.exe"
    }
    if (-not (Test-Path $psql)) {
        Write-Error "psql bulunamadi. PostgreSQL kurulu mu? PATH'e ekleyin."
        exit 1
    }
} else {
    $psql = $psql.Source
}

Write-Host "`n[1/3] Kullanici ve veritabani olusturuluyor..." -ForegroundColor Cyan
Write-Host "      psql: $psql" -ForegroundColor Gray
Write-Host "      (postgres kullanicisinin sifresini girmeniz istenecek)`n"

$sql = @"
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PG_USER') THEN
    CREATE USER $PG_USER WITH PASSWORD '$PG_PASS';
  END IF;
END
`$`$;
CREATE DATABASE $PG_DB OWNER $PG_USER;
GRANT ALL PRIVILEGES ON DATABASE $PG_DB TO $PG_USER;
"@

$sql | & $psql -U postgres -h $PG_HOST -p $PG_PORT

if ($LASTEXITCODE -ne 0) {
    Write-Warning "Veritabani zaten mevcut olabilir, devam ediliyor..."
}

Write-Host "`n[2/3] .env dosyasi DATABASE_URL guncelleniyor..." -ForegroundColor Cyan

$envFile = Join-Path $PSScriptRoot ".env"
$dbUrl = "postgresql://${PG_USER}:${PG_PASS}@${PG_HOST}:${PG_PORT}/${PG_DB}"

if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw
    if ($content -match "DATABASE_URL=") {
        $content = $content -replace "DATABASE_URL=.*", "DATABASE_URL=$dbUrl"
    } else {
        $content = "DATABASE_URL=$dbUrl`n" + $content
    }
    Set-Content $envFile $content -Encoding utf8
    Write-Host "      DATABASE_URL=$dbUrl" -ForegroundColor Green
} else {
    Write-Warning ".env bulunamadi, elle olusturun:"
    Write-Host "  DATABASE_URL=$dbUrl" -ForegroundColor Yellow
}

Write-Host "`n[3/3] Schema ve migration'lar calistiriliyor..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -c "from db import init_db; init_db()"

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ Kurulum tamamlandi!" -ForegroundColor Green
    Write-Host "   Uygulamayi baslatmak icin: .\start_app.bat" -ForegroundColor White
} else {
    Write-Error "Schema olusturma basarisiz. DATABASE_URL ve PostgreSQL baglantisini kontrol edin."
}
