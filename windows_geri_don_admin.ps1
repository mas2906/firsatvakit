# Ubuntu gecisini geri alir: Ubuntu servisini durdurur, Windows scraper gorevini yeniden acar.
# YONETICI olarak calistirilmali.

$ErrorActionPreference = 'Continue'
$log = 'D:\firsatvakti_new\logs\ubuntu_gecis.log'

function Say($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'HATA: yonetici olarak calistirilmali'
    exit 1
}

Say 'GERI DONUS: Ubuntu servisi durduruluyor'
wsl.exe -d Ubuntu -e systemctl --user disable --now firsatvakti-scraper
Start-Sleep -Seconds 3

$running = (wsl.exe -d Ubuntu -e sh -c "pgrep -fc 'local_scraper.py'") -join ''
if ($running.Trim() -ne '0') {
    Say "HATA: Ubuntu'da scraper hala calisiyor ($running), Windows baslatilmadi"
    exit 1
}

Enable-ScheduledTask -TaskName 'FirsatVakti-Scraper' | Out-Null
Start-ScheduledTask -TaskName 'FirsatVakti-Scraper'
Start-Sleep -Seconds 8
Say ("TAMAM: Windows gorevi durumu: {0}" -f (Get-ScheduledTask -TaskName 'FirsatVakti-Scraper').State)
