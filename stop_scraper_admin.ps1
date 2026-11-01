# FırsatVakti scraper'ını DURDURUR (yeniden başlatmaz) — paket/tarayıcı güncellemesi
# gibi, scraper dosyaları kilitliyken yapılamayan işler için.
# YÖNETİCİ olarak çalıştırılmalı. Tekrar başlatmak için: restart_scraper_admin.ps1

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
        ($_.Name -eq 'camoufox.exe') -or
        ($_.Name -eq 'node.exe'       -and $_.CommandLine -like '*playwright*')
    }
}

Write-Host "1) Zamanlanmış görev durduruluyor..."
Stop-ScheduledTask -TaskName 'FirsatVakti-Scraper' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

Write-Host "2) Scraper / watchdog / tarayıcı süreçleri kapatılıyor..."
foreach ($i in 1..3) {
    $procs = @(Get-ScraperProcs)
    if ($procs.Count -eq 0) { break }
    foreach ($p in $procs) {
        Write-Host ("   kapatılıyor: {0} {1}" -f $p.ProcessId, $p.Name)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}
$left = @(Get-ScraperProcs)
if ($left.Count -gt 0) { Write-Host "UYARI: $($left.Count) süreç hâlâ ayakta." -ForegroundColor Yellow }
else { Write-Host "TAMAM: scraper durduruldu." -ForegroundColor Green }
