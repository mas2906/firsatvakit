$ProjectDir = "D:\firsatvakti_new\firsatvakti_new"
$Python     = "$ProjectDir\.venv\Scripts\python.exe"
$Script     = "$ProjectDir\local_scraper.py"
$LogDir     = "D:\firsatvakti_new\logs"
$LogFile    = "$LogDir\scraper.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

log "=== run_scraper.ps1 basladi ==="

while ($true) {
    log "Scraper baslatiliyor..."
    try {
        $p = Start-Process -FilePath $Python `
            -ArgumentList $Script `
            -WorkingDirectory $ProjectDir `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput "$LogDir\scraper_out.log" `
            -RedirectStandardError  "$LogDir\scraper_err.log"
        $p.WaitForExit()
        $exit = $p.ExitCode
        log "Scraper durdu (exit=$exit). 5 saniye sonra yeniden basliyor..."
    } catch {
        log "Baslatilamaadi: $_"
    }
    Start-Sleep -Seconds 5
}
