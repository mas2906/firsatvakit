#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy sağlık kontrolü — arka planda periyodik olarak çalışır.

Kullanım:
    import asyncio
    from scrapers.proxy_health import start_health_checker

    async def main():
        task = await start_health_checker()   # arka planda başlar
        ...
        task.cancel()                         # durdurmak için
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("proxy_health")

# Kontrol edilen URL — sadece IP döner, veri sızdırmaz
_HEALTH_URL      = "https://httpbin.org/ip"
_CHECK_INTERVAL  = 120   # her 2 dakikada bir tam tur
_REQUEST_TIMEOUT = 8     # saniye


async def _check_one(proxy: str) -> tuple[bool, float]:
    """Tek bir proxy'yi test et. (ok, elapsed_ms) döner."""
    import httpx
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=_REQUEST_TIMEOUT) as c:
            r = await c.head(_HEALTH_URL)
        elapsed_ms = (time.monotonic() - t0) * 1000
        return r.status_code == 200, elapsed_ms
    except Exception:
        return False, 0.0


async def _health_loop() -> None:
    """Sürekli döngü — her turda tüm proxy'leri sırayla kontrol eder."""
    from scrapers.proxy_pool import get_proxy_pool
    pool = get_proxy_pool()

    while True:
        await asyncio.sleep(_CHECK_INTERVAL)

        if not pool.has_proxies:
            continue

        proxies = list(pool._proxies)
        log.debug(f"[proxy_health] {len(proxies)} proxy kontrol ediliyor")

        for proxy in proxies:
            ok, ms = await _check_one(proxy)
            if ok:
                await pool.mark_ok(proxy, elapsed_ms=ms)
                log.debug(f"[proxy_health] ✔ {proxy[:35]}… {ms:.0f}ms")
            else:
                await pool.mark_failed(proxy)
                log.warning(f"[proxy_health] ✗ {proxy[:35]}… erişilemiyor")

            # Proxy'ler arası küçük bekleme — health check trafiğini dağıt
            await asyncio.sleep(0.5)


_health_task: Optional[asyncio.Task] = None


async def start_health_checker() -> asyncio.Task:
    """
    Health checker'ı arka plan task olarak başlatır.
    Birden fazla çağrıda tek task korunur.
    """
    global _health_task
    if _health_task is None or _health_task.done():
        _health_task = asyncio.create_task(_health_loop())
        log.info(
            f"[proxy_health] Başlatıldı — her {_CHECK_INTERVAL}s'de bir kontrol"
        )
    return _health_task


def stop_health_checker() -> None:
    global _health_task
    if _health_task and not _health_task.done():
        _health_task.cancel()
        _health_task = None
        log.info("[proxy_health] Durduruldu")
