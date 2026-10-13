# FırsatVakti scraper'ını GÜVENLİ şekilde yeniden başlatır.
# YÖNETİCİ olarak çalıştırılmalı (Başlat → PowerShell → sağ tık → Yönetici olarak çalıştır):
#   powershell -ExecutionPolicy Bypass -File D:\firsatvakti_new\firsatvakti_new\restart_scraper_admin.ps1
#
# Neden gerekli: Görev "En yüksek ayrıcalıklarla" çalışıyor; normal (yönetici olmayan)
# oturum bu süreçlerin komut satırını göremiyor ve öldüremiyor. Açılışta başlayan eski
# kopya mutex'i tutmaya devam ederse yeni kod hiç yüklenmiyor (yeni kopya "zaten
# çalışıyor" deyip çıkıyor).

$ErrorActionPreference = 'Continue'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "HATA: Bu betik YÖNETİCİ olarak çalıştırılmalı." -ForegroundColor Red
    exit 1
}

function Get-ScraperProcs {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'python.exe'     -and $_.CommandLine -like '*local_scraper.py*') -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*run_scraper.ps1*') -or
        ($_.Name -eq 'camoufox.exe')
    }
}

Write-Host "0) Görev ayarları düzeltiliyor (ikinci watchdog'u önlemek için)..."
# run_scraper.ps1 kendi içinde 5 sn'de bir yeniden başlatıyor. Görev seviyesindeki
# RestartCount (5 × PT1M) bunun ÜSTÜNE ikinci bir watchdog doğuruyordu → iki kopya,
# 770 binden fazla "başlatılıyor/durdu" döngüsü (2026-10-12). Ayrıca aynı görevin
# ikinci kez başlatılması (MultipleInstances) engelleniyor.
try {
    $task = Get-ScheduledTask -TaskName 'FirsatVakti-Scraper' -ErrorAction Stop
    $task.Settings.RestartCount = 0
    $task.Settings.RestartInterval = $null
    $task.Settings.MultipleInstances = 'IgnoreNew'
    Set-ScheduledTask -InputObject $task | Out-Null
    Write-Host "   RestartCount=0, MultipleInstances=IgnoreNew"
} catch {
    Write-Host "   UYARI: görev ayarları değiştirilemedi: $($_.Exception.Message)" -ForegroundColor Yellow
}
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\FirsatVaktiScraper.vbs'
if (Test-Path $startup) {
    Write-Host "   UYARI: Başlangıç klasöründe eski başlatıcı var, kaldırılıyor: $startup" -ForegroundColor Yellow
    Move-Item $startup 'D:\firsatvakti_new\disabled_startup\' -Force -ErrorAction SilentlyContinue
}

Write-Host "1) Zamanlanmış görev durduruluyor..."
Stop-ScheduledTask -TaskName 'FirsatVakti-Scraper' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

Write-Host "2) Kalan scraper / watchdog / tarayıcı süreçleri kapatılıyor..."
foreach ($i in 1..3) {
    $procs = @(Get-ScraperProcs)
    if ($procs.Count -eq 0) { break }
    foreach ($p in $procs) {
        Write-Host ("   kapatılıyor: {0} {1} (başlangıç {2})" -f $p.ProcessId, $p.Name, $p.CreationDate)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

$left = @(Get-ScraperProcs)
if ($left.Count -gt 0) {
    Write-Host "HATA: Hâlâ $($left.Count) süreç ayakta, başlatma iptal edildi:" -ForegroundColor Red
    $left | Select-Object ProcessId, Name, CreationDate | Format-Table
    exit 1
}

Write-Host "3) Görev TEK sefer başlatılıyor..."
Start-ScheduledTask -TaskName 'FirsatVakti-Scraper'
Start-Sleep -Seconds 45

Write-Host "4) Doğrulama:"
$py = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*local_scraper.py*' })
Write-Host "   local_scraper python süreci: $($py.Count) (1 veya 2 olmalı: venv başlatıcısı + gerçek python)"
Get-Content 'D:\firsatvakti_new\logs\scraper.log' -Tail 3
$err = Get-Content 'D:\firsatvakti_new\logs\scraper_err.log' -Tail 300 -ErrorAction SilentlyContinue
if ($err -match 'pid-lock') {
    Write-Host "UYARI: pid-lock mesajı var — başka bir kopya hâlâ kilidi tutuyor." -ForegroundColor Yellow
} elseif ($err -match '\[cookies\]') {
    Write-Host "TAMAM: Yeni kod yüklendi —" -ForegroundColor Green
    $err | Select-String '\[cookies\]' | Select-Object -Last 2 | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "Bilgi: [cookies] satırı henüz yok; ilk iş çekildiğinde görünecek. scraper_err.log'u kontrol et."
}
