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


import asyncio
import logging
import subprocess
import time
import httpx
from datetime import datetime
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
}

SCHEDULE = {
    "trendyol":    list(range(0, 24)),
    "n11":         list(range(0, 24)),
    "amazon":      list(range(0, 24)),
    "hepsiburada": list(range(0, 24)),
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


def _calc_batch(concurrent: int) -> int:
    return concurrent * 2


from scrapers.router import scrape_product  # noqa: E402

_last_success: dict[str, float] = {}
_error_streak_logged: dict[str, bool] = {}
ERROR_RESET_SECS = 15 * 60  # 15 dakika


def _task_done_callback(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception() if not task.cancelled() else None
        if exc:
            log.error(f"[task] Yakalanmamış exception: {exc}")


async def process_job(job: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    async with sem:
        pid          = job["product_id"]
        url          = job["url"]
        platform     = job["platform"]
        price_only   = job.get("price_only", False)
        cached_image = job.get("cached_image") or None

        log.info(f"[{platform}] #{pid}{' [fiyat]' if price_only else ''}")
        # DevTools/gercek-veri olcumune gore kalibre edildi: bu deger rate
        # limiter bekleme suresini (RL max) + Playwright yedek denemesinin
        # kendi ic timeout'unu (crawlee_pw_scrape timeout=) kapsamali, yoksa
        # Playwright kendi suresini bile tamamlamadan disaridan oldururuluyor
        # (orn. eski trendyol: RL max 40s + PW 48s > eski timeout 40s).
        _timeout = (95 if platform == "trendyol"    else  # RL 40s + PW 48s
                    80 if platform == "amazon"       else  # RL 20s + PW 50s
                    65 if platform == "n11"          else  # RL 20s + PW 30s
                    90)                                    # hepsiburada: RL 15s + PW 55s
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


async def express_lane(platform: str, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    """priority=-1 işleri için hızlı şerit. Normal kuyrukla (poll_platform) aynı
    semaphore'u paylaşır — toplam eşzamanlı iş PLATFORM_CONCURRENT'i aşmaz."""
    conc = PLATFORM_CONCURRENT.get(platform, 2)
    running: set[asyncio.Task] = set()
    log.info(f"[{platform}/express] Başladı (concurrent={conc}, paylaşımlı)")

    while True:
        try:
            running.difference_update({t for t in running if t.done()})
            free = conc - len(running)
            if free == 0:
                await asyncio.sleep(0.5)
                continue

            r = await client.get(
                f"{BASE_URL}/api/pending-jobs?platform={platform}&limit={free}&priority=-1",
                headers={"X-Api-Key": API_KEY},
                timeout=10,
            )
            if r.status_code == 200:
                jobs = r.json()
                if jobs:
                    log.info(f"[{platform}/express] {len(jobs)} öncelikli iş")
                    for job in jobs:
                        task = asyncio.create_task(process_job(job, client, sem))
                        running.add(task)
                        task.add_done_callback(running.discard)
                        task.add_done_callback(_task_done_callback)
                    await asyncio.sleep(0.5)
                    continue
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"[{platform}/express] Hata: {e}")
            await asyncio.sleep(2)


async def poll_platform(platform: str, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    """Tek platform için polling döngüsü. express_lane ile aynı semaphore paylaşılır."""
    concurrent = 0
    batch = 0
    was_active = False
    running: set[asyncio.Task] = set()
    _empty_count = 0
    _last_success[platform] = time.time()
    _error_streak_logged[platform] = False

    while True:
        try:
            active = _is_active(platform)

            if not active:
                if was_active:
                    log.info(f"[{platform}] Planlanan saatlerin dışında — pasif")
                    was_active = False
                # Pasif modda sadece öncelikli işleri al — bu zaten express_lane'in
                # işi, burası ek güvence.
                try:
                    r = await client.get(
                        f"{BASE_URL}/api/pending-jobs?platform={platform}&limit=4&priority=-1",
                        headers={"X-Api-Key": API_KEY},
                        timeout=10,
                    )
                    if r.status_code == 200 and (urgent := r.json()):
                        for job in urgent:
                            task = asyncio.create_task(process_job(job, client, sem))
                            running.add(task)
                            task.add_done_callback(running.discard)
                            task.add_done_callback(_task_done_callback)
                        await asyncio.sleep(2)
                        continue
                except Exception:
                    pass
                await asyncio.sleep(10)
                continue

            if not was_active:
                concurrent = _calc_concurrent(platform)
                batch = _calc_batch(concurrent)
                running = set()
                was_active = True
                log.info(f"[{platform}] Aktif — concurrent={concurrent} batch={batch}")

            running.difference_update({t for t in running if t.done()})
            free = concurrent - len(running)
            if free == 0:
                await asyncio.sleep(1)
                continue

            limit = min(free, batch)
            r = await client.get(
                f"{BASE_URL}/api/pending-jobs?platform={platform}&limit={limit}",
                headers={"X-Api-Key": API_KEY},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"[{platform}] Poll {r.status_code}")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            jobs = r.json()
            if not jobs:
                _empty_count += 1
                if _empty_count % 10 == 1:
                    log.info(f"[{platform}] Kuyruk boş (#{_empty_count})")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            _empty_count = 0

            log.info(f"[{platform}] {len(jobs)} iş ({len(running)} çalışıyor)")
            for job in jobs:
                task = asyncio.create_task(process_job(job, client, sem))
                running.add(task)
                task.add_done_callback(running.discard)
                task.add_done_callback(_task_done_callback)

            await asyncio.sleep(0)

        except Exception as e:
            log.error(f"[{platform}] Poll hatası: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def _run_platform(platform: str, client: httpx.AsyncClient, fn, sem: asyncio.Semaphore) -> None:
    """Platform loop'unu süresiz çalıştır; crash sonrası 10s bekle."""
    while True:
        try:
            await fn(platform, client, sem)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error(f"[{platform}/{fn.__name__}] Beklenmeyen çıkış: {e} — 10s sonra yeniden")
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
        log.info(f"  {p}: concurrent={c} batch={c*2}")

    # Platform başına TEK semaphore — poll_platform ve express_lane paylaşır,
    # böylece gerçek eşzamanlı iş tavanı PLATFORM_CONCURRENT'i geçmez (önceden
    # her lane kendi semaphore'unu yaratıyordu, tavan fiilen 2 katıydı).
    sems = {p: asyncio.Semaphore(PLATFORM_CONCURRENT[p]) for p in platforms}
    clients = {p: httpx.AsyncClient(timeout=15) for p in platforms}
    init_client = httpx.AsyncClient(timeout=15)
    try:
        await reset_stale_jobs(init_client)
        await asyncio.gather(
            *[_run_platform(p, clients[p], poll_platform, sems[p]) for p in platforms],
            *[_run_platform(p, clients[p], express_lane,  sems[p]) for p in platforms],
        )
    finally:
        _release_pid_lock()
        await init_client.aclose()
        for c in clients.values():
            await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
