#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Retry motoru, üstel backoff ve domain başına concurrency limiti.

Proxy / IP rotasyonu YOK — zamanlama ve TLS fingerprint ile yönetilir.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Coroutine, Optional, TypeVar

from scrapers.models import ScraperConfig

log = logging.getLogger("scraper.engine")
T   = TypeVar("T")


# ═══════════════════════════════════════════════════════════════
# HATA TİPLERİ
# ═══════════════════════════════════════════════════════════════

class TerminalError(Exception):
    """
    Retry yapılmaması gereken hata.
    dead_url, not_found, 404, 410 gibi kalıcı durumlar için fırlatılır.
    RetryEngine bu exception'ı yakalayınca döngüyü anında durdurur.
    """


class BlockedError(Exception):
    """
    Anti-bot bloğu — retry yapılır ama daha uzun beklemeyle.
    RetryEngine normal backoff * 2 uygular.
    """


# ═══════════════════════════════════════════════════════════════
# BACKOFF STRATEJİSİ
# ═══════════════════════════════════════════════════════════════

class BackoffStrategy:
    """
    Üstel geri çekilme + jitter.

    wait(0) = base + jitter          → ~2.0–2.6s
    wait(1) = base*2 + jitter        → ~4.0–5.2s
    wait(2) = base*4 + jitter        → ~8.0–10.4s
    wait(n) = min(base*2^n, cap) + jitter

    Jitter: sabit bekleme → senkronize yük artışı (thundering herd) önler.
    """

    def __init__(self, base: float = 2.0, cap: float = 60.0, jitter: float = 0.3):
        self.base   = base
        self.cap    = cap
        self.jitter = jitter

    def wait(self, attempt: int, multiplier: float = 1.0) -> float:
        raw   = min(self.base * (2 ** attempt) * multiplier, self.cap)
        noise = raw * self.jitter * random.random()
        return raw + noise


# ═══════════════════════════════════════════════════════════════
# DOMAIN LİMİTER — domain başına Semaphore
# ═══════════════════════════════════════════════════════════════

class DomainLimiter:
    """
    Domain başına eş zamanlı istek sayısını sınırlar.
    Semaphore'lar lazily oluşturulur — asyncio event loop'u hazır olduğunda.

    Kullanım:
        async with limiter.limit("www.trendyol.com"):
            result = await fetch(url)
    """

    def __init__(self, concurrency: int = 2):
        self._n    = concurrency
        self._sems: dict[str, asyncio.Semaphore] = {}

    def _get_sem(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._sems:
            self._sems[domain] = asyncio.Semaphore(self._n)
        return self._sems[domain]

    def limit(self, domain: str) -> "_SemCtx":
        return _SemCtx(self, domain)


class _SemCtx:
    """DomainLimiter.limit() için async context manager."""

    def __init__(self, limiter: DomainLimiter, domain: str):
        self._limiter = limiter
        self._domain  = domain
        self._sem: Optional[asyncio.Semaphore] = None

    async def __aenter__(self) -> "_SemCtx":
        self._sem = self._limiter._get_sem(self._domain)
        await self._sem.acquire()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._sem is not None:
            self._sem.release()


# Tüm scraper'ların paylaştığı singleton limiter
_GLOBAL_LIMITER: Optional[DomainLimiter] = None


def get_limiter(concurrency: int = 2) -> DomainLimiter:
    """Singleton DomainLimiter döndür. İlk çağrıda oluşturulur."""
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is None:
        _GLOBAL_LIMITER = DomainLimiter(concurrency)
    return _GLOBAL_LIMITER


# ═══════════════════════════════════════════════════════════════
# RETRY ENGİNE
# ═══════════════════════════════════════════════════════════════

class RetryEngine:
    """
    Coroutine factory'yi retry + üstel backoff ile çalıştır.

    Davranışlar:
      coro_fn() → T (None olmayan)  : başarı, döngü biter
      coro_fn() → None              : geçici hata, retry tetiklenir
      coro_fn() raises TerminalError: kalıcı hata, hemen çıkılır (retry yok)
      coro_fn() raises BlockedError : blok, 2x backoff ile retry
      coro_fn() raises Exception    : beklenmeyen hata, normal backoff ile retry

    IP rotasyonu yok — sadece zamanlama (backoff + jitter) kullanılır.
    """

    def __init__(self, config: ScraperConfig):
        self.cfg     = config
        self.backoff = BackoffStrategy(
            base   = config.backoff_base,
            cap    = config.backoff_max,
            jitter = config.jitter_factor,
        )

    async def run(
        self,
        coro_fn: Callable[[], Coroutine[Any, Any, Optional[T]]],
        *,
        label: str = "",
    ) -> Optional[T]:
        """
        coro_fn: argümansız, Optional[T] döndüren coroutine factory.

        Args:
            coro_fn: Her denemede çağrılacak coroutine üretici.
            label:   Log mesajlarında görünecek kısa tanım (platform:url).

        Returns:
            İlk başarılı sonuç veya max_retries tükendiyse None.
        """
        for attempt in range(self.cfg.max_retries + 1):
            try:
                result = await coro_fn()

                if result is not None:
                    if attempt > 0:
                        log.info(f"[{label}] retry #{attempt} başarılı")
                    return result

                # None → geçici hata, retry
                if attempt < self.cfg.max_retries:
                    wait = self.backoff.wait(attempt)
                    log.info(
                        f"[{label}] deneme {attempt + 1}/{self.cfg.max_retries} → "
                        f"None döndü, {wait:.1f}s bekleniyor"
                    )
                    await asyncio.sleep(wait)

            except TerminalError as e:
                log.debug(f"[{label}] terminal hata ({e}) — retry yok")
                return None

            except BlockedError as e:
                if attempt < self.cfg.max_retries:
                    wait = self.backoff.wait(attempt, multiplier=2.0)
                    log.warning(f"[{label}] blok tespiti ({e}) → 2x backoff {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    log.error(f"[{label}] blok, tüm denemeler tükendi")

            except Exception as e:
                if attempt < self.cfg.max_retries:
                    wait = self.backoff.wait(attempt)
                    log.warning(
                        f"[{label}] {e.__class__.__name__}: {e} → {wait:.1f}s bekleniyor"
                    )
                    await asyncio.sleep(wait)
                else:
                    log.error(f"[{label}] tüm denemeler başarısız: {e}")

        log.warning(f"[{label}] max_retries={self.cfg.max_retries} tükendi")
        return None
