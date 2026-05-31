#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy başına cookie jar yönetimi.

Sticky session kullanan platformlarda (Amazon, HepsiB) aynı proxy ile
devam ederken önceki istekten kalan cookie'leri taşımak gerekir.
Aksi hâlde her istekte yeni session sayılır ve Akamai / PerimeterX
challenge'ı yeniden tetiklenir.

Kullanım — curl_cffi:
    store = get_cookie_store()
    store.inject_to_curl(session, proxy)
    r = await session.get(url, ...)
    store.update_from_curl(session, proxy)

Kullanım — Playwright:
    await store.inject_to_playwright(context, proxy, ".hepsiburada.com")
    await page.goto(url)
    await store.update_from_playwright(context, proxy)
"""

import asyncio
import logging
from typing import Optional

log = logging.getLogger("cookie_store")


class CookieStore:
    def __init__(self):
        # proxy_url → {name: value}
        self._jars: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    # ── Okuma ─────────────────────────────────────────────────────

    async def get(self, proxy: str) -> dict[str, str]:
        async with self._lock:
            return dict(self._jars.get(proxy, {}))

    # ── Güncelleme ─────────────────────────────────────────────────

    async def update(self, proxy: str, cookies: dict[str, str]) -> None:
        if not cookies:
            return
        async with self._lock:
            jar = self._jars.setdefault(proxy, {})
            jar.update(cookies)
            log.debug(f"[cookie_store] {proxy[:30]}… +{len(cookies)} cookie")

    async def invalidate(self, proxy: str) -> None:
        """Proxy değişince veya session sona erince cookie'leri temizle."""
        async with self._lock:
            removed = self._jars.pop(proxy, {})
            if removed:
                log.debug(f"[cookie_store] {proxy[:30]}… {len(removed)} cookie temizlendi")

    # ── curl_cffi entegrasyonu ─────────────────────────────────────

    async def inject_to_curl(self, session, proxy: str) -> None:
        """Kayıtlı cookie'leri curl_cffi session'ına yükle."""
        cookies = await self.get(proxy)
        for name, value in cookies.items():
            try:
                session.cookies.set(name, value)
            except Exception as e:
                log.debug(f"[cookie_store] cookie set hatası {name}: {e}")

    async def update_from_curl(self, session, proxy: str) -> None:
        """curl_cffi yanıtından cookie'leri kaydet."""
        try:
            cookies = {k: v for k, v in session.cookies.items()}
            await self.update(proxy, cookies)
        except Exception as e:
            log.debug(f"[cookie_store] curl cookie okunamadı: {e}")

    # ── Playwright entegrasyonu ────────────────────────────────────

    async def inject_to_playwright(
        self, context, proxy: str, domain: str
    ) -> None:
        """Kayıtlı cookie'leri Playwright context'ine yükle."""
        cookies = await self.get(proxy)
        if not cookies:
            return
        pw_cookies = [
            {"name": k, "value": v, "domain": domain, "path": "/"}
            for k, v in cookies.items()
        ]
        try:
            await context.add_cookies(pw_cookies)
            log.debug(
                f"[cookie_store] {proxy[:30]}… {len(pw_cookies)} cookie Playwright'a yüklendi"
            )
        except Exception as e:
            log.debug(f"[cookie_store] Playwright inject hatası: {e}")

    async def update_from_playwright(self, context, proxy: str) -> None:
        """Playwright context cookie'lerini kaydet."""
        try:
            pw_cookies = await context.cookies()
            cookies = {c["name"]: c["value"] for c in pw_cookies}
            await self.update(proxy, cookies)
        except Exception as e:
            log.debug(f"[cookie_store] Playwright cookie okunamadı: {e}")


# Modül seviyesinde singleton
_store: Optional[CookieStore] = None


def get_cookie_store() -> CookieStore:
    global _store
    if _store is None:
        _store = CookieStore()
    return _store
