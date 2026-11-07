# FirsatVakti - VPS veritabani yedegini bilgisayara ceker (haftada bir, 2026-11-07).
# VPS her gece 06:00 TR'de yedek alir (/root/firsatvakti/backup.sh, son 7 gun durur).
# Bu betik en yeni yedegi indirir, VPS'teki sha256 ozetiyle karsilastirir, son 8 haftayi saklar.
# Zamanlanmis gorev: "FirsatVakti-DB-Yedek" (Pazar 10:00, bilgisayar kapaliysa acilinca calisir).
# Log: D:\firsatvakti_new\backups\db\cekme.log

$ErrorActionPreference = 'Stop'
$Dest   = 'D:\firsatvakti_new\backups\db'
$LogF   = Join-Path $Dest 'cekme.log'
$Keep   = 8
$Ssh    = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$Scp    = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
$Remote = '/root/firsatvakti/backups'

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
function Write-Log($msg) {
    Add-Content -Path $LogF -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $msg) -Encoding UTF8
}

try {
    $latest = (& $Ssh -o BatchMode=yes -o ConnectTimeout=20 firsatvakti-vps "ls -1t $Remote/firsatvakti_*.sql.gz | head -n 1")
    if ($LASTEXITCODE -ne 0 -or -not $latest) { throw "VPS'e baglanilamadi ya da yedek yok (kod $LASTEXITCODE)" }
    $latest = $latest.Trim()
    $name   = Split-Path $latest -Leaf
    $local  = Join-Path $Dest $name

    $remoteHash = (& $Ssh -o BatchMode=yes firsatvakti-vps "cat $latest.sha256 2>/dev/null || sha256sum $latest | cut -d' ' -f1")
    if ($LASTEXITCODE -ne 0 -or -not $remoteHash) { throw "VPS'te ozet okunamadi" }
    $remoteHash = $remoteHash.Trim().ToLower()

    if ((Test-Path $local) -and ((Get-FileHash $local -Algorithm SHA256).Hash.ToLower() -eq $remoteHash)) {
        Write-Log "zaten var ve dogru: $name"
    } else {
        $part = "$local.part"
        & $Scp -q -o BatchMode=yes "firsatvakti-vps:$latest" $part
        if ($LASTEXITCODE -ne 0) { throw "indirme basarisiz (scp kod $LASTEXITCODE)" }
        $h = (Get-FileHash $part -Algorithm SHA256).Hash.ToLower()
        if ($h -ne $remoteHash) { Remove-Item $part -Force; throw "ozet uyusmuyor, dosya silindi: $name" }
        Move-Item $part $local -Force
        $mb = [math]::Round((Get-Item $local).Length / 1MB, 1)
        Write-Log "indirildi: $name ($mb MB), ozet dogru"
    }

    $old = Get-ChildItem $Dest -Filter 'firsatvakti_*.sql.gz' | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
    foreach ($f in $old) { Remove-Item $f.FullName -Force; Write-Log "eski yedek silindi: $($f.Name)" }
    exit 0
} catch {
    Write-Log "HATA: $_"
    exit 1
}
