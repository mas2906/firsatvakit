#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WSL Local Scraper — platform başına bağımsız polling döngüsü

import os
import sys
import tempfile as _tempfile


if sys.platform == "win32":
    _TMP = r"D:\firsatvakti_temp"
else:
    _TMP = "/tmp/firsatvakti_temp"
os.makedirs(_TMP, exist_ok=True)
os.environ["TEMP"] = _TMP
os.environ["TMP"] = _TMP
os.environ["TMPDIR"] = _TMP
_tempfile.tempdir = _TMP

import shutil

_CRAWLEE_DIR = os.path.join(_TMP, "crawlee_storage")
_PROJECT_STORAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
os.environ["CRAWLEE_STORAGE_DIR"] = _CRAWLEE_DIR


def _reset_crawlee_storage() -> None:
    """Crawlee disk queue birikimini önle: temiz bir depoyla başla.
    PID kilidi alındıktan SONRA çağrılmalı — aksi halde kilidi kaybedip hemen
    çıkacak bir kopya süreç bile bu paylaşılan dizini silip asıl sürecin
    ayağına dolanabilir (geçmişte tam olarak buna bağlı çökmeler yaşandı)."""
    shutil.rmtree(_CRAWLEE_DIR, ignore_errors=True)
    os.makedirs(_CRAWLEE_DIR, exist_ok=True)
    # Proje içindeki eski storage dizinini de temizle (önceki çalışmanın kalıntısı)
    shutil.rmtree(_PROJECT_STORAGE, ignore_errors=True)


# Playwright/Camoufox her taramada TEMP altında yeni bir tarayıcı profili açıyor
# (içinde cookies.sqlite dahil) ama kapanınca silmiyor — binlerce klasör birikiyordu.
_BROWSER_PROFILE_PREFIXES = (
    "apify-playwright-firefox-", "apify-playwright-chrom", "playwright_chromiumdev_profile-",
    "playwright_firefoxdev_profile-", "playwright-artifacts-", "rust_mozprofile",
)


def _purge_browser_profiles(min_age_s: float = 600) -> int:
    """TEMP altındaki eski tarayıcı profillerini (ve içlerindeki çerezleri) siler.
    min_age_s'den yeni klasörlere dokunulmaz — o an çalışan bir tarayıcıya ait
    olabilir (Windows kullanımdaki dosyaları zaten kilitliyor, ignore_errors)."""
    removed = 0
    cutoff = time.time() - min_age_s
    try:
        entries = list(os.scandir(_TMP))
    except OSError:
        return 0
    for e in entries:
        if not e.is_dir(follow_symlinks=False) or not e.name.startswith(_BROWSER_PROFILE_PREFIXES):
            continue
        try:
            if e.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(e.path, ignore_errors=True)
        if not os.path.exists(e.path):
            removed += 1
    return removed


import asyncio
import logging
import subprocess
import time
import httpx
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("local_scraper")

BASE_URL      = os.getenv("BASE_URL", "https://firsatvakti.com")
API_KEY       = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY   = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECS", "1"))

# Platform başına eş zamanlı iş sayısı — ürünler günde bir kez tarandığı için
# (bkz. scheduler.py) hız değil güvenlik önceliklidir, bilerek düşük tutulur.
# Trendyol kataloğu küçük olduğu için (bkz. /admin) daha da düşük — daha önce
# yüksek concurrency + kısa gecikmeyle blok yemişti.
PLATFORM_CONCURRENT = {
    "amazon":      2,
    "trendyol":    1,
    "hepsiburada": 2,
    "n11":         2,
    "karaca":      1,
}

SCHEDULE = {
    "trendyol":    list(range(0, 24)),
    "n11":         list(range(0, 24)),
    "amazon":      list(range(0, 24)),
    "hepsiburada": list(range(0, 24)),
    "karaca":      list(range(0, 24)),
}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _is_active(platform: str) -> bool:
    return datetime.now().hour in SCHEDULE[platform]


def _is_night() -> bool:
    return 0 <= datetime.now().hour < 7


def _calc_concurrent(platform: str) -> int:
    base = PLATFORM_CONCURRENT.get(platform, 1)
    if platform == "amazon" and _is_night():
        return base * 2
    return base


from scrapers.router import scrape_product  # noqa: E402
from scrapers.crawlee_base import RL  # noqa: E402


# ── Tarama turu takibi: tur başa döndüğünde çerezleri temizle ─────────────────
# VPS /api/pending-jobs yanıtında X-Scan-Round başlığıyla mevcut turun kimliğini
# gönderiyor (scheduler.py → settings.scan_round_started_at).
_scan_round: Optional[str] = None
_round_lock: Optional[asyncio.Lock] = None


async def _check_scan_round(round_id: Optional[str]) -> None:
    global _scan_round, _round_lock
    if not round_id or round_id == _scan_round:
        return
    if _round_lock is None:
        _round_lock = asyncio.Lock()
    async with _round_lock:
        if round_id == _scan_round:     # başka bir worker az önce halletti
            return
        previous, _scan_round = _scan_round, round_id
        if previous is None:            # süreç yeni başladı — çerezler zaten boş
            log.info(f"[cookies] Mevcut tarama turu: {round_id}")
            return
        from scrapers.crawlee_base import reset_all_sessions
        sessions = await reset_all_sessions()
        profiles = await asyncio.to_thread(_purge_browser_profiles)
        log.info(f"[cookies] 🔄 Yeni tarama turu ({previous} → {round_id}): "
                 f"{sessions} curl oturumu sıfırlandı, {profiles} eski tarayıcı profili silindi — tüm çerezler temizlendi")

_last_success: dict[str, float] = {}
_error_streak_logged: dict[str, bool] = {}
ERROR_RESET_SECS = 15 * 60  # 15 dakika


async def process_job(job: dict, client: httpx.AsyncClient) -> None:
    """Tek işi baştan sona çalıştırır. Eşzamanlılık artık worker sayısıyla
    (bkz. _worker) doğal olarak sınırlı — burada ayrıca semaphore'a gerek yok."""
    pid          = job["product_id"]
    url          = job["url"]
    platform     = job["platform"]
    price_only   = job.get("price_only", False)
    cached_image = job.get("cached_image") or None

    log.info(f"[{platform}] #{pid}{' [fiyat]' if price_only else ''}")

    # RL.wait() bilerek dis timeout'un DISINDA cagriliyor: RateLimiter hem
    # adaptif backoff (blok sinyalinde araligi 4 kata kadar genisletiyor)
    # hem de %12 ihtimalle "insan gibi uzun duraklama" ekliyor — ikisi
    # birlestiginde tek basina RL bekleme suresi dis timeout butcesini
    # asabiliyordu. Bu durumda is asyncio.wait_for tarafindan RL.wait()
    # icindeyken iptal ediliyordu; bu da ne record_success() ne de
    # record_failure() cagrilmadigi icin RL'in asla iyilesemeyecegi
    # sekilde kalici olarak yuksek kalmasina yol aciyordu (n11'de tam
    # olarak bu yasandi — surec yeniden baslatilana kadar duzelmiyordu).
    # RL.wait() burada, timeout disinda oldugu icin ne kadar surerse
    # sursun isi oldurmuyor; asagidaki _timeout artik sadece asil scrape
    # denemesinin (curl+PW) kendi suresini kapsamak zorunda.
    await RL[platform].wait()

    # DevTools/gercek-veri olcumune gore kalibre edildi: curl on-denemesi
    # (varsa, session.get timeout=) + Playwright'in kendi ic timeout'u
    # (crawlee_pw_scrape timeout=) + guvenlik payi.
    _timeout = (80 if platform == "trendyol"    else  # PW 65s (isinma dahil, curl yok)
                80 if platform == "amazon"       else  # PW 65s (isinma dahil, curl yok)
                90 if platform == "n11"          else  # curl 20s + PW 55s
                90)                                    # hepsiburada: curl 20s + PW 55s
    try:
        data = await asyncio.wait_for(
            scrape_product(url, platform, price_only=price_only, cached_image=cached_image),
            timeout=_timeout,
        )

        if data and (data.get("dead_url") or data.get("not_found")):
            log.warning(f"[{platform}] ☠ #{pid} ölü URL: {url[:60]}")
            await send_webhook(client, pid, url, platform, {"error": "dead_url"})
            return

        if data and data.get("price"):
            log.info(f"[{platform}] ✔ #{pid} fiyat={data['price']} stok={data.get('stock','-')}")
            _last_success[platform] = time.time()
            _error_streak_logged[platform] = False
        else:
            log.warning(f"[{platform}] ✘ #{pid} veri yok — {url[:60]}")

        await send_webhook(client, pid, url, platform, data or {})
    except asyncio.TimeoutError:
        log.error(f"[{platform}] TIMEOUT #{pid}")
        await send_webhook(client, pid, url, platform, {"error": "timeout"})
    except Exception as e:
        log.error(f"[{platform}] HATA #{pid}: {e}")
        await send_webhook(client, pid, url, platform, {"error": str(e)})


async def send_webhook(client: httpx.AsyncClient, product_id: int, url: str,
                        platform: str, data: dict) -> None:
    payload = {"product_id": product_id, "url": url, "platform": platform, "data": data}
    try:
        r = await client.post(
            f"{BASE_URL}/api/scraper-webhook",
            json=payload,
            headers={"X-Webhook-Key": WEBHOOK_KEY},
            timeout=10,
        )
        log.info(f"[{platform}] webhook {r.status_code} #{product_id}")
    except Exception as e:
        log.error(f"[{platform}] webhook failed #{product_id}: {e}")


async def reset_stale_jobs(client: httpx.AsyncClient) -> None:
    try:
        r = await client.post(
            f"{BASE_URL}/api/reset-stale-jobs",
            headers={"X-Api-Key": API_KEY},
            params={"force": "false"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            log.info(f"[startup] Sıkışık işler: {data.get('reset', 0)} pending'e döndü")
        else:
            log.warning(f"[startup] reset-stale-jobs: {r.status_code}")
    except Exception as e:
        log.warning(f"[startup] reset-stale-jobs başarısız: {e}")


async def _fetch_one_job(client: httpx.AsyncClient, platform: str, urgent_only: bool = False) -> Optional[dict]:
    """Önce öncelikli (admin/kullanıcı ekledi, priority=-1) bir iş var mı bak,
    yoksa normal kuyruktan bir iş al. İkisi de limit=1: worker boşaldığı an
    tek bir iş isteyip hemen çalışmaya başlar — batch/poll aralığı beklemesi
    sadece kuyruk gerçekten boşken (aşağıdaki _worker'daki sleep) devreye girer.
    urgent_only=True: sadece priority=-1 denenir (pasif saatlerde kullanılır)."""
    param_sets = [{"platform": platform, "limit": "1", "priority": "-1"}]
    if not urgent_only:
        param_sets.append({"platform": platform, "limit": "1"})
    for params in param_sets:
        try:
            r = await client.get(
                f"{BASE_URL}/api/pending-jobs",
                params=params,
                headers={"X-Api-Key": API_KEY},
                timeout=10,
            )
            if r.status_code == 200:
                await _check_scan_round(r.headers.get("X-Scan-Round"))
                jobs = r.json()
                if jobs:
                    return jobs[0]
        except Exception as e:
            log.debug(f"[{platform}] pending-jobs isteği başarısız: {e}")
    return None


async def _worker(platform: str, worker_id: int, client: httpx.AsyncClient) -> None:
    """Tek bir eşzamanlı çalışma slotu. İş biter bitmez —hiç ara vermeden—
    bir sonraki işi ister; sadece kuyruk gerçekten boşken kısa bir bekleme
    devreye girer. Eskiden ayrı poll_platform + express_lane döngüleri
    paylaşımlı bir semaphore üzerinden aynı işi yapıyordu — artık her worker
    kendi başına bir slot, ekstra senkronizasyona gerek yok."""
    was_active = False
    empty_count = 0
    log.info(f"[{platform}/{worker_id}] Başladı")

    while True:
        try:
            if not _is_active(platform):
                if was_active:
                    log.info(f"[{platform}/{worker_id}] Planlanan saatlerin dışında — pasif")
                    was_active = False
                # Pasif modda sadece öncelikli (priority=-1) işler alınmaya devam eder.
                job = await _fetch_one_job(client, platform, urgent_only=True)
                if job:
                    await process_job(job, client)
                    continue
                await asyncio.sleep(10)
                continue

            if not was_active:
                was_active = True
                log.info(f"[{platform}/{worker_id}] Aktif")

            # Amazon gece saatlerinde (0-7) ekstra worker'larla (_calc_concurrent
            # ile 2 katına çıkar) çalışır — gündüz bu ekstra worker'lar (worker_id
            # temel PLATFORM_CONCURRENT'i aşıyorsa) bekleme moduna geçer.
            if worker_id >= _calc_concurrent(platform):
                await asyncio.sleep(60)
                continue

            job = await _fetch_one_job(client, platform)
            if not job:
                empty_count += 1
                if empty_count % 20 == 1:
                    log.info(f"[{platform}/{worker_id}] Kuyruk boş (#{empty_count})")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            empty_count = 0

            await process_job(job, client)
            # İş bitti — hiç beklemeden döngü başına dönüp bir sonrakini iste.

        except Exception as e:
            log.error(f"[{platform}/{worker_id}] Hata: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def _run_worker(platform: str, worker_id: int, client: httpx.AsyncClient) -> None:
    """Worker'ı süresiz çalıştır; beklenmeyen bir çöküş olursa 10s bekleyip yeniden başlat."""
    while True:
        try:
            await _worker(platform, worker_id, client)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error(f"[{platform}/{worker_id}] Beklenmeyen çıkış: {e} — 10s sonra yeniden")
            await asyncio.sleep(10)


_PID_FILE = os.path.join(_TMP, "firsatvakti_scraper.pid")
_MUTEX_NAME = "Global\\FirsatVaktiScraperSingleInstance_v2"
_ERROR_ALREADY_EXISTS = 183
_win_mutex_handle = None


def _pid_alive(pid: int) -> bool:
    """Verilen PID çalışıyor mu? (sadece dosya-tabanlı fallback için kullanılır,
    Windows'ta asıl kilit mekanizması artık _acquire_pid_lock_win altındaki
    named mutex — bkz. orada ki yorum)."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return f'"{pid}"' in r.stdout
        else:
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True  # belirsizse güvenli taraf: canlı kabul et


def _acquire_pid_lock_file() -> bool:
    """Dosya tabanlı yedek kilit (Windows dışı platformlar veya mutex
    başarısız olursa). O_CREAT|O_EXCL atomik ama tasklist kontrolüne
    dayandığı için Windows'ta tek başına yeterince güvenilir değil —
    asıl mekanizma _acquire_pid_lock_win."""
    my_pid = os.getpid()
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
        except Exception:
            old_pid = None
        if old_pid is not None and old_pid != my_pid and _pid_alive(old_pid):
            log.warning(f"[pid-lock] Scraper zaten çalışıyor (PID={old_pid}) — çıkılıyor")
            return False
        try:
            os.remove(_PID_FILE)
        except Exception:
            pass
    try:
        fd = os.open(_PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(my_pid).encode())
        os.close(fd)
        return True
    except FileExistsError:
        log.warning("[pid-lock] Race condition — başka instance başladı")
        return False


def _acquire_pid_lock() -> bool:
    """Tek örnek garantisi. Windows'ta işletim sistemi seviyesinde ATOMİK bir
    named mutex kullanılır (CreateMutexW) — dosya + tasklist kontrolüne göre
    iki temel avantajı var:
      1. TOCTOU yarışı imkansız (tek kernel çağrısı, "kontrol et sonra yaz"
         iki adımlı deseni yok) — daha önce dosya-tabanlı kilit, aynı saniye
         içinde başlayan iki watchdog tetikleyicisinde (AtStartup+AtLogOn)
         her ikisinin de kilidi "kazandığını" sanmasına yol açmıştı.
      2. Süreç çökse/zorla kapatılsa bile mutex OS tarafından otomatik
         serbest bırakılır — "eski PID'yi manuel temizle" derdi kalmaz.
    ÖNEMLİ: Mutex'i varsayılan (NULL) güvenlik tanımlayıcısıyla oluşturmak,
    farklı yetki seviyesindeki (elevated/standart) süreçlerin aynı mutex'e
    erişimini ENGELLİYOR (UAC split-token) — bu yüzden burada açıkça
    "Everyone/World" erişimine izin veren bir SDDL tanımlayıcısı kullanılıyor.
    Bu olmadan, watchdog'un yükseltilmiş yetkiyle başlattığı bir kopya ile
    farklı bağlamda başlayan başka bir kopya birbirini göremiyor, ikisi de
    kilidi "kazandığını" sanıp aynı platforma paralel istek atabiliyordu.
    Windows dışı sistemlerde (ya da mutex API'si her nedense başarısız
    olursa) dosya tabanlı yönteme düşülür."""
    global _win_mutex_handle
    if sys.platform != "win32":
        return _acquire_pid_lock_file()

    try:
        import ctypes
        from ctypes import wintypes

        class _SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        kernel32.CreateMutexW.argtypes = [ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        # "D:(A;;GA;;;WD)" = herkese (World) tam erişim (Generic All) izni ver.
        psd = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:(A;;GA;;;WD)", 1, ctypes.byref(psd), None):
            raise OSError(f"SDDL dönüştürme başarısız (hata={ctypes.get_last_error()})")

        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = psd
        sa.bInheritHandle = False

        handle = kernel32.CreateMutexW(ctypes.byref(sa), False, _MUTEX_NAME)
        err = ctypes.get_last_error()
        kernel32.LocalFree(psd)
    except Exception as e:
        log.warning(f"[pid-lock] Windows mutex API kullanılamadı ({e}) — dosya tabanlı kilide düşülüyor")
        return _acquire_pid_lock_file()

    if not handle:
        log.warning(f"[pid-lock] Mutex oluşturulamadı (hata={err}) — dosya tabanlı kilide düşülüyor")
        return _acquire_pid_lock_file()
    if err == _ERROR_ALREADY_EXISTS:
        log.warning("[pid-lock] Scraper zaten çalışıyor (mutex sahipli) — çıkılıyor")
        kernel32.CloseHandle(handle)
        return False

    _win_mutex_handle = handle
    return True


def _release_pid_lock() -> None:
    global _win_mutex_handle
    if _win_mutex_handle:
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_win_mutex_handle)
        except Exception:
            pass
        _win_mutex_handle = None
        return
    try:
        os.remove(_PID_FILE)
    except Exception:
        pass




async def main() -> None:
    if not _acquire_pid_lock():
        return
    _reset_crawlee_storage()

    platforms = list(PLATFORM_CONCURRENT)
    total_c = sum(PLATFORM_CONCURRENT.values())
    log.info(f"WSL Local Scraper (crawlee) başladı — toplam concurrent={total_c}")
    for p, c in PLATFORM_CONCURRENT.items():
        log.info(f"  {p}: concurrent={c}")

    # Her worker = tek bir eşzamanlı slot (bkz. _worker). Amazon gece
    # saatlerinde (_calc_concurrent) 2 katına çıkabildiği için o kadar worker
    # baştan doğuruyoruz — gündüz fazlalar kendi içlerinde bekleme moduna geçer.
    worker_counts = {p: (c * 2 if p == "amazon" else c) for p, c in PLATFORM_CONCURRENT.items()}
    clients = {p: httpx.AsyncClient(timeout=15) for p in platforms}
    init_client = httpx.AsyncClient(timeout=15)
    try:
        await reset_stale_jobs(init_client)
        tasks = [
            _run_worker(p, wid, clients[p])
            for p in platforms
            for wid in range(worker_counts[p])
        ]
        await asyncio.gather(*tasks)
    finally:
        _release_pid_lock()
        await init_client.aclose()
        for c in clients.values():
            await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
