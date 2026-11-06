# FirsatVakti scraper'ini Windows'ta durdurur ve otomatik baslamasini kapatir (Ubuntu'ya gecis).
# YONETICI olarak calistirilmali. Buybox uygulamasina (port 8000) dokunmaz.
# Geri donus: windows_geri_don_admin.ps1

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

function Get-ScraperProcs {
    $all = Get-CimInstance Win32_Process
    $py = @($all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*local_scraper.py*' })
    $pyIds = @($py | ForEach-Object { $_.ProcessId })
    $wrap = @($all | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*run_scraper.ps1*' })
    $node = @($all | Where-Object { $_.Name -eq 'node.exe' -and $pyIds -contains $_.ParentProcessId })
    $cam = @($all | Where-Object { $_.Name -eq 'camoufox.exe' })
    return @($wrap) + @($py) + @($node) + @($cam)
}

Say 'ADIM 1: Windows scraper durduruluyor'
Stop-ScheduledTask -TaskName 'FirsatVakti-Scraper' -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName 'FirsatVakti-Scraper' -ErrorAction Continue | Out-Null
Start-Sleep -Seconds 3

foreach ($i in 1..5) {
    $procs = @(Get-ScraperProcs)
    if ($procs.Count -eq 0) { break }
    Say ("kapatiliyor: {0} surec" -f $procs.Count)
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}

$left = @(Get-ScraperProcs)
$state = (Get-ScheduledTask -TaskName 'FirsatVakti-Scraper').State
Say ("kalan surec: {0} | gorev durumu: {1}" -f $left.Count, $state)

if ($left.Count -eq 0 -and "$state" -eq 'Disabled') {
    Say 'TAMAM: Windows scraper durdu ve otomatik baslama kapali'
    exit 0
}
Say 'HATA: Windows tarafi tam durmadi, Ubuntu baslatilmamali'
exit 1
