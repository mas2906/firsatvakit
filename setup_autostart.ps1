# FirsatVakti Scraper - Windows Task Scheduler ile Otomatik Baslatma
# Bu scripti YONETİCİ olarak BİR KERE calistirin.

$TaskName    = "FirsatVakti-Scraper"
$WatchdogPs1 = "D:\firsatvakti_new\firsatvakti_new\run_scraper.ps1"
$LogDir      = "D:\firsatvakti_new\logs"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

# Mevcut gorevi temizle
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[*] Eski gorev silindi."
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WatchdogPs1`"" `
    -WorkingDirectory "D:\firsatvakti_new\firsatvakti_new"

# Startup + Logon tetikleyicisi (her ikisi de)
$TriggerStartup = New-ScheduledTaskTrigger -AtStartup
$TriggerLogon   = New-ScheduledTaskTrigger -AtLogOn

$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     @($TriggerStartup, $TriggerLogon) `
    -Principal   $Principal `
    -Settings    $Settings `
    -Description "FirsatVakti local scraper - otomatik baslatma + watchdog" | Out-Null

Write-Host ""
Write-Host "=== KURULUM TAMAMLANDI ===" -ForegroundColor Green
Write-Host "Gorev adi  : $TaskName"
Write-Host "Tetikleyici: Windows acilisinda + kullanici girişinde"
Write-Host "Watchdog   : Scraper durursa 5 saniye sonra otomatik yeniden baslar"
Write-Host ""
Write-Host "SIMDI baslatmak icin:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
