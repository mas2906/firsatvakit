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
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("proxy_pool")

_PROXY_FILE = os.path.join(os.path.dirname(__file__), "..", "proxies.txt")

# Proxy başarısız sayılmadan önce kaç saniye ceza verilir
_FAIL_COOLDOWN = 300   # 5 dakika
# Bir proxy'nin ardışık kaç başarısızlık sonrası geçici olarak devre dışı bırakılır
_MAX_CONSECUTIVE_FAILS = 3


class ProxyPool:
    def __init__(self):
        self._proxies: list[str] = self._load()
        self._fail_counts: dict[str, int] = {}
        self._fail_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        if self._proxies:
            log.info(f"[ProxyPool] {len(self._proxies)} proxy yüklendi")
        else:
            log.info("[ProxyPool] Proxy tanımlanmamış — direkt IP ile bağlanılacak")

    def _load(self) -> list[str]:
        proxies: list[str] = []

        # .env / os.environ
        env_val = os.getenv("PROXY_LIST", "").strip()
        if env_val:
            for p in env_val.split(","):
                p = p.strip()
                if p:
                    proxies.append(p)

        # proxies.txt
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

        # Tekrar edenleri temizle, sırayı koru
        seen: set = set()
        unique: list[str] = []
        for p in proxies:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    def get(self) -> Optional[str]:
        """Kullanılabilir bir proxy döner. Proxy yoksa None (direkt bağlantı)."""
        if not self._proxies:
            return None
        now = time.monotonic()
        available = [
            p for p in self._proxies
            if now >= self._fail_until.get(p, 0)
        ]
        if not available:
            # Tüm proxiler cezalıysa — hepsinin cezasını sıfırla ve rastgele seç
            self._fail_until.clear()
            self._fail_counts.clear()
            log.warning("[ProxyPool] Tüm proxiler başarısız — listeler sıfırlanıyor")
            return random.choice(self._proxies)
        return random.choice(available)

    def mark_failed(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        self._fail_counts[proxy] = self._fail_counts.get(proxy, 0) + 1
        count = self._fail_counts[proxy]
        if count >= _MAX_CONSECUTIVE_FAILS:
            cooldown = _FAIL_COOLDOWN * min(count - _MAX_CONSECUTIVE_FAILS + 1, 4)
            self._fail_until[proxy] = time.monotonic() + cooldown
            log.warning(
                f"[ProxyPool] Proxy {_mask(proxy)} {cooldown}s askıya alındı "
                f"({count} ardışık hata)"
            )
        else:
            log.info(f"[ProxyPool] Proxy {_mask(proxy)} hata #{count}")

    def mark_ok(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        self._fail_counts.pop(proxy, None)
        self._fail_until.pop(proxy, None)

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

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
