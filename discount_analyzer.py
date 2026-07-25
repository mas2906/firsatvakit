#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fiyat analiz modülü — gerçek / şüpheli / sahte indirim tespiti.

Kullanım:
    analyzer = DiscountAnalyzer(db_url="postgresql://...")
    result   = await analyzer.analyze(product_id=123)
    results  = await analyzer.analyze_bulk(platform="trendyol", min_discount=10)

Bağımlılık: asyncpg  (pip install asyncpg)
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

log = logging.getLogger("discount_analyzer")

VALID_PLATFORMS = {"trendyol", "amazon", "hepsiburada", "n11"}

# ── Eşik sabitleri ────────────────────────────────────────────────────────────

# Spike: 7-14 gün penceresi baseline'dan bu kadar yüksekse yapay artış sayılır
_SPIKE_RATIO = 0.12          # %12

# GERÇEK: mevcut fiyat 90g ortalamasının bu oranının altında olmalı
_REAL_BELOW_AVG90 = 0.90     # %90

# SAHTE: mevcut fiyat 30g ortalamasının üstündeyse kesin sahte
_FAKE_ABOVE_AVG30 = 1.00

# ŞÜPHELİ: gerçek indirim bu oranın altındaysa şüphe var
_SUSPICIOUS_REAL_RATE = 0.05  # %5


# ── Veri sınıfı ───────────────────────────────────────────────────────────────

@dataclass
class DiscountResult:
    product_id: int
    platform: str
    current_price: float
    original_price: Optional[float]       # platformun gösterdiği eski fiyat
    displayed_discount_rate: Optional[float]  # platformun gösterdiği indirim %
    avg_30d: Optional[float]
    avg_60d: Optional[float]
    avg_90d: Optional[float]
    min_90d: Optional[float]
    real_discount_rate: Optional[float]   # 90g ortalamaya göre gerçek indirim oranı
    price_spike_before_discount: bool     # indirimden önce yapay artış var mı
    verdict: str                          # GERÇEK / ŞÜPHELİ / SAHTE
    confidence: float                     # 0-1 arası güven skoru
    data_quality: str                     # yeterli / az veri / çok az veri / veri yok
    history_days: int                     # kaç günlük fiyat verisi mevcut

    def to_dict(self) -> dict:
        return asdict(self)


# ── Ana sınıf ─────────────────────────────────────────────────────────────────

class DiscountAnalyzer:
    """
    PostgreSQL price_history tablosundan gerçek indirim analizi yapar.

    Context manager olarak veya doğrudan kullanılabilir:
        async with DiscountAnalyzer(db_url) as a:
            result = await a.analyze(123)
    """

    def __init__(self, db_url: str, pool_min: int = 1, pool_max: int = 5):
        self._db_url = db_url
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Optional[asyncpg.Pool] = None

    # ── Yaşam döngüsü ─────────────────────────────────────────────────────────

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._db_url,
                min_size=self._pool_min,
                max_size=self._pool_max,
            )
        return self._pool

    async def close(self) -> None:
        """Bağlantı havuzunu kapat."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self):
        await self._get_pool()
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ── Veritabanı sorguları ──────────────────────────────────────────────────

    async def _fetch_product(self, conn, product_id: int) -> Optional[dict]:
        """Ürün ve aktif deal bilgisini çek."""
        row = await conn.fetchrow(
            """
            SELECT
                p.id,
                p.platform,
                p.title,
                d.old_price,
                d.new_price,
                d.discount_pct,
                d.created_at::timestamp AS deal_created_at
            FROM products p
            LEFT JOIN deals d
                ON d.product_id = p.id
               AND d.active = 1
            WHERE p.id = $1
            """,
            product_id,
        )
        return dict(row) if row else None

    async def _fetch_price_history(
        self, conn, product_id: int, days: int = 90
    ) -> list[dict]:
        """Son N gün fiyat geçmişini kronolojik sırayla çek."""
        rows = await conn.fetch(
            """
            SELECT
                price_value,
                scraped_at::timestamp AS scraped_at
            FROM price_history
            WHERE product_id = $1
              AND scraped_at::timestamp >= NOW() - ($2 || ' days')::interval
            ORDER BY scraped_at ASC
            """,
            product_id,
            str(days),
        )
        return [dict(r) for r in rows]

    async def _fetch_platform_product_ids(
        self, conn, platform: str, min_discount: float, limit: int
    ) -> list[int]:
        """Aktif deal'i olan platform ürünlerinin id listesini çek."""
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.id
            FROM products p
            JOIN deals d
                ON d.product_id = p.id
               AND d.active = 1
            WHERE p.platform = $1
              AND d.discount_pct >= $2
            ORDER BY p.id
            LIMIT $3
            """,
            platform,
            min_discount,
            limit,
        )
        return [r["id"] for r in rows]

    # ── Hesaplama yardımcıları ────────────────────────────────────────────────

    @staticmethod
    def _to_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    def _calculate_averages(self, history: list[dict]) -> dict:
        """30 / 60 / 90 günlük ortalama ve 90 günlük minimum hesapla."""
        now = datetime.now(timezone.utc)
        result: dict = {}

        for label, days in (("30d", 30), ("60d", 60), ("90d", 90)):
            cutoff = now - timedelta(days=days)
            prices = [
                r["price_value"]
                for r in history
                if self._to_utc(r["scraped_at"]) >= cutoff
            ]
            result[f"avg_{label}"] = round(sum(prices) / len(prices), 2) if prices else None

        all_prices = [r["price_value"] for r in history]
        result["min_90d"] = round(min(all_prices), 2) if all_prices else None
        return result

    def _detect_spike(self, history: list[dict]) -> bool:
        """
        İndirimden 7-14 gün önceki fiyat artışını (yapay şişirme) tespit et.

        Yöntem:
          - spike_window : son 7-14 günlük maksimum fiyat
          - baseline     : 15-30 gün önceki ortalama fiyat
          Eğer spike_window > baseline * (1 + _SPIKE_RATIO) → spike var.
        """
        now = datetime.now(timezone.utc)

        def _in(r, start, end) -> bool:
            ts = self._to_utc(r["scraped_at"])
            return start <= ts <= end

        spike_w = [
            r["price_value"] for r in history
            if _in(r, now - timedelta(days=14), now - timedelta(days=7))
        ]
        baseline = [
            r["price_value"] for r in history
            if _in(r, now - timedelta(days=30), now - timedelta(days=15))
        ]

        if not spike_w or not baseline:
            return False

        avg_baseline = sum(baseline) / len(baseline)
        return max(spike_w) > avg_baseline * (1 + _SPIKE_RATIO)

    def _compute_verdict(
        self,
        current_price: float,
        avgs: dict,
        spike: bool,
        real_discount_rate: Optional[float],
    ) -> tuple[str, float]:
        """
        Verdict ve güven skoru üret.

        Skor pozitife yaklaştıkça SAHTE, negatife yaklaştıkça GERÇEK.
        """
        avg_30d = avgs.get("avg_30d")
        avg_90d = avgs.get("avg_90d")
        min_90d = avgs.get("min_90d")

        score = 0.0

        # ── SAHTE sinyalleri ──────────────────────────────────────────────────
        if avg_30d is not None and current_price > avg_30d * _FAKE_ABOVE_AVG30:
            # Mevcut fiyat 30g ortalamasının üzerinde → en güçlü sahte sinyali
            score += 0.45
        if spike:
            score += 0.30
        if real_discount_rate is not None and 0 < real_discount_rate < _SUSPICIOUS_REAL_RATE:
            # Çok küçük gerçek indirim → şüphe
            score += 0.15
        if real_discount_rate is not None and real_discount_rate <= 0:
            # Negatif: mevcut fiyat ortalamadan yüksek
            score += 0.25

        # ── GERÇEK sinyalleri ─────────────────────────────────────────────────
        if avg_90d is not None and current_price < avg_90d * _REAL_BELOW_AVG90:
            score -= 0.35
        if min_90d is not None and current_price < min_90d:
            # Yeni tarihsel dip → güçlü gerçek sinyali
            score -= 0.25
        if not spike:
            score -= 0.10

        # ── Verdict ───────────────────────────────────────────────────────────
        if score >= 0.35:
            verdict = "SAHTE"
        elif score <= -0.25:
            verdict = "GERÇEK"
        else:
            verdict = "ŞÜPHELİ"

        # Güven skoru: farkın büyüklüğü + minimum taban
        confidence = round(min(1.0, abs(score) + 0.30), 2)
        return verdict, confidence

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyze(
        self,
        product_id: int,
        platform: str = "",
    ) -> dict:
        """
        Tek ürün için fiyat analizi yap.

        Args:
            product_id : products.id
            platform   : opsiyonel — ürünün platformuyla eşleşmiyorsa uyarı verir

        Returns:
            DiscountResult.to_dict() veya hata durumunda {"product_id": ..., "error": ...}
        """
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                product = await self._fetch_product(conn, product_id)
                if not product:
                    log.warning(f"[analyzer] Ürün bulunamadı: #{product_id}")
                    return {"product_id": product_id, "error": "ürün bulunamadı"}

                if platform and product["platform"] != platform:
                    log.warning(
                        f"[analyzer] Platform uyuşmazlığı #{product_id}: "
                        f"beklenen={platform}, gerçek={product['platform']}"
                    )

                history = await self._fetch_price_history(conn, product_id, days=90)

            # ── Veri yoksa erken dön ──────────────────────────────────────────
            if not history:
                log.info(f"[analyzer] #{product_id} — fiyat geçmişi yok")
                return {
                    "product_id": product_id,
                    "platform": product["platform"],
                    "data_quality": "veri yok",
                    "verdict": "ŞÜPHELİ",
                    "confidence": 0.0,
                    "history_days": 0,
                }

            # ── Hesaplamalar ──────────────────────────────────────────────────
            current_price = history[-1]["price_value"]
            first_ts = self._to_utc(history[0]["scraped_at"])
            history_days = (datetime.now(timezone.utc) - first_ts).days

            avgs = self._calculate_averages(history)
            spike = self._detect_spike(history)

            real_discount_rate: Optional[float] = None
            if avgs["avg_90d"] and avgs["avg_90d"] > 0:
                real_discount_rate = round(
                    (avgs["avg_90d"] - current_price) / avgs["avg_90d"], 4
                )

            verdict, confidence = self._compute_verdict(
                current_price, avgs, spike, real_discount_rate
            )

            if history_days >= 30:
                data_quality = "yeterli"
            elif history_days >= 7:
                data_quality = "az veri"
            else:
                data_quality = "çok az veri"

            result = DiscountResult(
                product_id=product_id,
                platform=product["platform"],
                current_price=current_price,
                original_price=product.get("old_price"),
                displayed_discount_rate=product.get("discount_pct"),
                avg_30d=avgs["avg_30d"],
                avg_60d=avgs["avg_60d"],
                avg_90d=avgs["avg_90d"],
                min_90d=avgs["min_90d"],
                real_discount_rate=real_discount_rate,
                price_spike_before_discount=spike,
                verdict=verdict,
                confidence=confidence,
                data_quality=data_quality,
                history_days=history_days,
            )

            log.info(
                f"[analyzer] #{product_id} {product['platform']} "
                f"fiyat={current_price:.0f}₺ verdict={verdict} conf={confidence:.2f} "
                f"spike={spike} gerçek_indirim={real_discount_rate and f'%{real_discount_rate*100:.1f}'}"
            )
            return result.to_dict()

        except Exception as exc:
            log.error(f"[analyzer] #{product_id} analiz hatası: {exc}", exc_info=True)
            return {"product_id": product_id, "error": str(exc)}

    async def analyze_bulk(
        self,
        platform: str,
        min_discount: float = 10.0,
        limit: int = 500,
        concurrency: int = 20,
    ) -> list[dict]:
        """
        Platform bazında toplu analiz — aktif deal'i olan ürünleri işler.

        Args:
            platform    : trendyol / amazon / hepsiburada / n11
            min_discount: platformun gösterdiği minimum indirim yüzdesi filtresi
            limit       : maksimum ürün sayısı
            concurrency : eş zamanlı analiz sayısı (DB bağlantı havuzunu aşmamak için)

        Returns:
            DiscountResult listesi — SAHTE önce, sonra ŞÜPHELİ, sonra GERÇEK
        """
        if platform not in VALID_PLATFORMS:
            raise ValueError(f"Geçersiz platform: '{platform}'. Geçerliler: {VALID_PLATFORMS}")

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            product_ids = await self._fetch_platform_product_ids(
                conn, platform, min_discount, limit
            )

        log.info(
            f"[analyzer] Toplu analiz — platform={platform} "
            f"min_discount=%{min_discount} adet={len(product_ids)}"
        )

        # Semaphore ile eşzamanlılığı sınırla
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(pid: int) -> dict:
            async with sem:
                return await self.analyze(pid, platform)

        results = await asyncio.gather(*[_bounded(pid) for pid in product_ids])

        _order = {"SAHTE": 0, "ŞÜPHELİ": 1, "GERÇEK": 2}
        return sorted(
            [r for r in results if "error" not in r],
            key=lambda r: _order.get(r.get("verdict", "ŞÜPHELİ"), 1),
        )
