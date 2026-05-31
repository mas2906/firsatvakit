#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy rotation pool — curl_cffi ve Playwright için ortak proxy yönetimi.

proxies.txt formatı (proje kök dizininde):
  http://username:password@hostname:port
  http://username:password@hostname:port
  # Yorum satırları # ile başlar

.env ile de tanımlanabilir:
  PROXY_LIST=http://user:pass@host:port,http://user2:pass2@host2:port2

Rotating proxy servisleri (IPRoyal, Bright Data, SmartProxy) genellikle
tek bir endpoint verir, satırın kendisi zaten her istekte farklı IP döner:
  http://user:pass@geo.iproyal.com:12321
"""

import os
import random
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("proxy_pool")

_PROXY_FILE = os.path.join(os.path.dirname(__file__), "..", "proxies.txt")

_FAIL_COOLDOWN      = 300   # saniye — 3. hatada başlayan bekleme
_MAX_CONSECUTIVE    = 3     # kaç ardışık hata sonrası cooldown
_STICKY_DEFAULT_TTL = 480   # saniye — varsayılan sticky session ömrü


# ─── Per-proxy istatistik ────────────────────────────────────────────

@dataclass
class ProxyStats:
    success:   int   = 0
    failure:   int   = 0
    total_ms:  float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_ms / self.success if self.success else 0.0

    @property
    def success_rate(self) -> float:
        total = self.success + self.failure
        return self.success / total if total else 1.0


# ─── Sticky session kaydı ────────────────────────────────────────────

@dataclass
class StickySession:
    proxy:      str
    platform:   str
    created_at: float = field(default_factory=time.monotonic)
    ttl:        float = _STICKY_DEFAULT_TTL

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) >= self.ttl


# ─── Ana sınıf ───────────────────────────────────────────────────────

class ProxyPool:
    # Platform bazlı sticky TTL (saniye). 0 = rotating (her istekte yeni IP).
    _PLATFORM_TTL: dict[str, float] = {
        "amazon":      600,   # 10 dk — PerimeterX cookie zinciri
        "hepsiburada": 360,   # 6 dk  — Akamai _abck sensor verisi IP'ye bağlı
        "trendyol":    0,     # rotating — Cloudflare stateless
        "n11":         0,     # rotating — GraphQL API, cookie gerektirmez
    }

    def __init__(self):
        self._proxies:  list[str]                = self._load()
        self._fail_counts: dict[str, int]        = {}
        self._fail_until:  dict[str, float]      = {}
        self._stats:    dict[str, ProxyStats]    = {}
        self._sessions: dict[str, StickySession] = {}  # platform → aktif session
        self._lock = asyncio.Lock()
        if self._proxies:
            log.info(f"[ProxyPool] {len(self._proxies)} proxy yüklendi")
        else:
            log.info("[ProxyPool] Proxy tanımlanmamış — direkt IP ile bağlanılacak")

    def _load(self) -> list[str]:
        proxies: list[str] = []

        env_val = os.getenv("PROXY_LIST", "").strip()
        if env_val:
            for p in env_val.split(","):
                p = p.strip()
                if p:
                    proxies.append(p)

        try:
            with open(os.path.abspath(_PROXY_FILE), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        proxies.append(line)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[ProxyPool] proxies.txt okunamadı: {e}")

        seen: set = set()
        unique: list[str] = []
        for p in proxies:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    # ── İç seçim mantığı ─────────────────────────────────────────────

    def _pick_available(self) -> Optional[str]:
        """Lock altında çağrılmalı."""
        if not self._proxies:
            return None
        now = time.monotonic()
        available = [p for p in self._proxies if now >= self._fail_until.get(p, 0)]
        if not available:
            self._fail_until.clear()
            self._fail_counts.clear()
            log.warning("[ProxyPool] Tüm proxiler başarısız — listeler sıfırlanıyor")
            return random.choice(self._proxies)
        return self._weighted_pick(available)

    def _weighted_pick(self, candidates: list[str]) -> str:
        """
        Başarı oranı yüksek + latency düşük proxy'yi tercih eder.
        İstatistik yoksa nötr skor (1.0) → eşit ağırlık.
        """
        if len(candidates) == 1:
            return candidates[0]

        def _score(p: str) -> float:
            s = self._stats.get(p)
            if not s or s.success == 0:
                return 1.0
            latency_score = max(0.0, 1.0 - s.avg_latency_ms / 3000.0)
            return s.success_rate * 0.7 + latency_score * 0.3

        # Skoru ağırlık olarak kullanarak olasılıklı seçim — deterministik değil
        scores = [_score(p) for p in candidates]
        total  = sum(scores) or 1.0
        r      = random.random() * total
        cum    = 0.0
        for p, s in zip(candidates, scores):
            cum += s
            if r <= cum:
                return p
        return candidates[-1]

    # ── Public API ───────────────────────────────────────────────────

    async def get(self) -> Optional[str]:
        """Rotating proxy — her çağrıda mevcut en iyi proxy döner."""
        if not self._proxies:
            return None
        async with self._lock:
            return self._pick_available()

    async def get_sticky(self, platform: str) -> Optional[str]:
        """
        Platform için sticky session proxy döner.

        TTL=0 platformlar (Trendyol, N11) → rotating get() kullanılır.
        TTL>0 platformlar (Amazon, HepsiB) → aynı IP session boyunca korunur.
        """
        ttl = self._PLATFORM_TTL.get(platform, 0)
        if ttl == 0:
            return await self.get()

        async with self._lock:
            session = self._sessions.get(platform)
            if session and not session.expired:
                # Mevcut session proxy'si hâlâ sağlıklıysa devam et
                now = time.monotonic()
                if now >= self._fail_until.get(session.proxy, 0):
                    return session.proxy
                # Proxy cezalıysa yeni session aç
                log.info(
                    f"[ProxyPool] {platform} sticky proxy cezalı — yeni session"
                )

            proxy = self._pick_available()
            if proxy:
                self._sessions[platform] = StickySession(
                    proxy=proxy, platform=platform, ttl=ttl
                )
                log.debug(
                    f"[ProxyPool] {platform} yeni sticky session: {_mask(proxy)} "
                    f"(TTL={ttl}s)"
                )
            return proxy

    async def mark_ok(self, proxy: Optional[str], elapsed_ms: float = 0.0) -> None:
        if not proxy:
            return
        async with self._lock:
            self._fail_counts.pop(proxy, None)
            self._fail_until.pop(proxy, None)
            s = self._stats.setdefault(proxy, ProxyStats())
            s.success  += 1
            s.total_ms += elapsed_ms

    async def mark_failed(self, proxy: Optional[str], platform: str = "") -> None:
        if not proxy:
            return
        async with self._lock:
            self._fail_counts[proxy] = self._fail_counts.get(proxy, 0) + 1
            count = self._fail_counts[proxy]
            s = self._stats.setdefault(proxy, ProxyStats())
            s.failure += 1

            # Sticky session'ı geçersiz kıl — yenisi açılacak
            sess = self._sessions.get(platform)
            if platform and sess and sess.proxy == proxy:
                del self._sessions[platform]

            if count >= _MAX_CONSECUTIVE:
                cooldown = _FAIL_COOLDOWN * min(count - _MAX_CONSECUTIVE + 1, 4)
                self._fail_until[proxy] = time.monotonic() + cooldown
                log.warning(
                    f"[ProxyPool] {_mask(proxy)} {cooldown}s askıya alındı "
                    f"({count} ardışık hata)"
                )
            else:
                log.info(f"[ProxyPool] {_mask(proxy)} hata #{count}")

    # ── Metrik raporu ─────────────────────────────────────────────────

    def stats_report(self) -> list[dict]:
        """Tüm proxy'lerin performans özeti — log veya monitoring export için."""
        report = []
        for proxy, s in self._stats.items():
            report.append({
                "proxy":          _mask(proxy),
                "success":        s.success,
                "failure":        s.failure,
                "success_rate":   round(s.success_rate, 3),
                "avg_latency_ms": round(s.avg_latency_ms, 1),
            })
        return sorted(report, key=lambda x: x["success_rate"], reverse=True)

    def log_stats(self) -> None:
        for row in self.stats_report():
            log.info(
                f"[ProxyPool/stats] {row['proxy']} "
                f"ok={row['success_rate']:.1%} "
                f"avg={row['avg_latency_ms']:.0f}ms "
                f"({row['success']}✓ {row['failure']}✗)"
            )

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

    # ── Format dönüşümleri ────────────────────────────────────────────

    @staticmethod
    def playwright_dict(proxy_url: Optional[str]) -> Optional[dict]:
        """Playwright'ın beklediği formatı döner."""
        if not proxy_url:
            return None
        try:
            parsed = urlparse(proxy_url)
            server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            result: dict = {"server": server}
            if parsed.username:
                result["username"] = parsed.username
            if parsed.password:
                result["password"] = parsed.password
            return result
        except Exception:
            return {"server": proxy_url}

    @staticmethod
    def curl_dict(proxy_url: Optional[str]) -> Optional[dict]:
        """curl_cffi'nin beklediği {'http': ..., 'https': ...} formatı."""
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}


def _mask(url: str) -> str:
    """Log için şifreyi gizle: http://user:XXXX@host:port"""
    try:
        p = urlparse(url)
        if p.password:
            return url.replace(p.password, "****")
    except Exception:
        pass
    return url[:40]


# Modül seviyesinde singleton
_pool: Optional[ProxyPool] = None


def get_proxy_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        _pool = ProxyPool()
    return _pool
