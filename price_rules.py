#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fiyat kuralları — fırsat eşiği, fiyat doğrulama, yo-yo ve "önce zam sonra indirim" tespiti.

2026-09-30 fiyat analizinin (4.651 ürün, 191 bin kayıt) sonuçlarına göre:
  * %10+ fiyat düşüşlerinin %78'i son 30 günün en düşük fiyatının altına inmiyordu —
    fiyat yakın zamanda zaten gördüğü seviyeye geri dönüyordu. Artık bir düşüş ancak
    30 günlük dibin altına iniyorsa fırsat sayılır (Ticaret Bakanlığı indirim
    yönetmeliğinin mantığı) ve indirim oranı 30 günlük MEDYANA göre hesaplanır.
  * 637 ürün "yo-yo" fiyatlanıyor (iki seviye arasında sürekli gidip geliyor); bunlarda
    düşüş fiyat en az YOYO_CONFIRM_HOURS sabit kaldıktan sonra fırsat sayılır.
  * n11'de iki fiyat yapışıp milyarlık fiyatlar kaydediliyordu; 30 günlük medyanın
    4 katı dışındaki fiyatlar kaydedilmeden önce yeniden taranır.

Fonksiyonlar saf (DB'ye dokunmaz): girdi olarak [(zaman_str, fiyat), ...] alır.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Optional, Sequence

MIN_DISCOUNT_PCT = 10.0      # 30 günlük medyana göre en az bu kadar indirim
# Platforma özel eşik (2026-10-12 kullanıcı kararı): Karaca'da indirimler küçük ve
# seyrek (çoğu %3-15) — %10 eşiğiyle hiç fırsat çıkmıyordu. "30 günlük dibin altına
# inme" şartı aynen geçerli; açılıp kapanan aynı indirim yine tekrar bildirilmez.
PLATFORM_MIN_DISCOUNT_PCT = {"karaca": 3.0}


def min_discount_for(platform: Optional[str]) -> float:
    return PLATFORM_MIN_DISCOUNT_PCT.get(platform or "", MIN_DISCOUNT_PCT)
NEW_LOW_TOLERANCE = 0.99     # fiyat, 30 günlük dibin en az %1 altında olmalı
# Fiyat dilimi kuralı (2026-11-07 kullanıcı kararı, yalnız Amazon): fiyat 30 günlük dibin
# altına inmese de son 30 günün en ucuz %10'luk süresindeyse ve medyana göre en az %20
# ucuzsa aday olur. Geçmiş 30 günde 249 ürünlük mevcut kurala 10 ürün ekledi; "dibe %3
# yakınlık" da 10 ürün ekliyordu ama sürenin %29'unda görülmüş fiyatları da geçiriyordu.
# {platform: (en fazla dilim %, medyana göre en az indirim %)}
PERCENTILE_RULE = {"amazon": (10.0, 20.0)}
# Ürünün fiyatı en az bu kadar gündür biliniyor olmalı. 7 → 3 (2026-10-11, kullanıcı
# kararı): Karaca'nın açıp kapattığı indirimlerin İLK açılışı yeni ürünlerde 7 gün
# dolmadan geliyordu ve kaçıyordu (ör. Crusty 2.999 → 2.549 TL, 5. gün). Tekrarları
# "30 günlük dibin altına inmedi" kuralı elemeye devam eder.
MIN_KNOWN_DAYS = 3
SANITY_FACTOR = 4.0          # medyanın 4 katı / 4'te biri dışı şüpheli
SANITY_MIN_DAYS = 3
YOYO_CONFIRM_HOURS = 12
_FMT = "%Y-%m-%d %H:%M:%S"

Point = tuple  # (scraped_at: str, price: float)


def _ts(s) -> datetime:
    return s if isinstance(s, datetime) else datetime.strptime(str(s)[:19], _FMT)


def _norm(points: Sequence[Point]) -> list:
    return sorted(((_ts(t), float(p)) for t, p in points if p and float(p) > 0), key=lambda x: x[0])


def _window(points: Sequence[Point], end: datetime, days: int) -> list:
    """[end-days, end) aralığındaki değişim noktaları + pencere başında geçerli olan fiyat.
    price_history sadece fiyat DEĞİŞİNCE kayıt tutar; 40 gündür sabit bir fiyat pencerede
    hiç kayıt bırakmaz — bu yüzden pencere başındaki fiyat başa eklenir (zamanı = start)."""
    start = end - timedelta(days=days)
    pts = _norm(points)
    carry = [p for t, p in pts if t < start]
    inside = [(t, p) for t, p in pts if start <= t < end]
    return ([(start, carry[-1])] if carry else []) + inside


def _segments(win: list, end: datetime) -> list:
    """Her fiyatın pencere içinde geçerli kaldığı süre: [(fiyat, saat), ...]"""
    out = []
    for i, (t, p) in enumerate(win):
        nxt = win[i + 1][0] if i + 1 < len(win) else end
        out.append((p, max((nxt - t).total_seconds() / 3600, 0.0)))
    return out


def _weighted_median(segs: list) -> Optional[float]:
    total = sum(h for _, h in segs)
    if total <= 0:
        return median([p for p, _ in segs]) if segs else None
    acc = 0.0
    for p, h in sorted(segs):
        acc += h
        if acc >= total / 2:
            return p
    return segs[-1][0]


def _price_percentile(segs: list, price: float) -> float:
    """Pencerenin yüzde kaçında fiyat bu seviye veya altındaydı (süre ağırlıklı)."""
    total = sum(h for _, h in segs)
    if total <= 0:
        return 100.0
    return sum(h for p, h in segs if p <= price) / total * 100


def _known_days(win: list, end: datetime) -> float:
    return (end - win[0][0]).total_seconds() / 86400 if win else 0.0


def _compress(points) -> list:
    """Ardışık aynı fiyatları tek kayda indirir (değişim noktaları)."""
    out = []
    for t, p in points:
        if not out or out[-1][1] != p:
            out.append((t, p))
    return out


# ── Fiyat doğrulama ────────────────────────────────────────────────────────────
def is_price_sane(points: Sequence[Point], price: float, now: datetime) -> tuple[bool, Optional[float]]:
    """Yeni fiyat son 30 günün (süre ağırlıklı) medyanına göre makul mü? (ok, medyan)"""
    win = _window(points, now, 30)
    if _known_days(win, now) < SANITY_MIN_DAYS:
        return True, None
    m = _weighted_median(_segments(win, now))
    if not m:
        return True, None
    return (m / SANITY_FACTOR <= price <= m * SANITY_FACTOR), m


# ── Yo-yo ─────────────────────────────────────────────────────────────────────
def is_yoyo(points: Sequence[Point], now: datetime) -> bool:
    """Son 30 günde haftada ≥2 değişim ve değişimlerin ≥%40'ı bir önceki fiyata geri dönüş."""
    cp = _compress([(t, p) for t, p in _norm(points) if now - timedelta(days=30) <= t <= now])
    changes = len(cp) - 1
    if changes < 6:
        return False
    span_days = max((cp[-1][0] - cp[0][0]).total_seconds() / 86400, 1)
    flips = sum(1 for i in range(2, len(cp)) if cp[i][1] == cp[i - 2][1])
    return changes / (span_days / 7) >= 2 and flips >= 0.4 * changes


# ── Fırsat kararı ─────────────────────────────────────────────────────────────
@dataclass
class DealDecision:
    is_deal: bool
    ref_price: Optional[float] = None   # 30 günlük medyan → deals.old_price olarak gösterilir
    pct: float = 0.0                    # medyana göre indirim %
    min30: Optional[float] = None
    reason: str = ""


def evaluate_drop(points: Sequence[Point], price: float, at: datetime,
                  min_pct: float = MIN_DISCOUNT_PCT, platform: Optional[str] = None) -> DealDecision:
    """`at` anında görülen `price` bir fırsat mı? Pencere, `at`'ten ÖNCEKİ 30 gün
    (medyan süre ağırlıklı: 29 gün geçerli kalan fiyat, 1 saatlik fiyattan ağır basar)."""
    win = _window(points, at, 30)
    known = _known_days(win, at)
    if known < MIN_KNOWN_DAYS:
        return DealDecision(False, reason=f"fiyat geçmişi yetersiz ({known:.1f} gün)")
    segs = _segments(win, at)
    min30, med30 = min(p for p, _ in segs), _weighted_median(segs)
    pct = (med30 - price) / med30 * 100 if med30 else 0.0
    if price >= min30 * NEW_LOW_TOLERANCE:
        rule = PERCENTILE_RULE.get(platform or "")
        if rule and pct >= rule[1]:
            low_pct = _price_percentile(segs, price)
            if low_pct <= rule[0]:
                return DealDecision(True, med30, pct, min30,
                                    f"30 günün en ucuz %{low_pct:.0f} diliminde, medyana göre %{pct:.1f}")
        return DealDecision(False, med30, pct, min30,
                            f"30 günlük dibin altına inmedi (dip={min30:.2f}, fiyat={price:.2f})")
    if pct < min_pct:
        return DealDecision(False, med30, pct, min30,
                            f"30 günlük medyana göre sadece %{pct:.1f} (medyan={med30:.2f})")
    return DealDecision(True, med30, pct, min30, f"yeni 30 günlük dip, medyana göre %{pct:.1f}")


# ── Önce zam, sonra indirim ────────────────────────────────────────────────────
@dataclass
class PumpEvent:
    date: datetime
    before: float       # zam öncesi fiyat
    raised: float       # şişirilmiş fiyat
    discounted: float   # "indirimli" fiyat
    days_high: float


def find_pump_events(points: Sequence[Point]) -> list[PumpEvent]:
    """Fiyat ≥7 gün bir seviyede → ≥%15 zam, 3–60 gün tutuluyor → ≥%15 indirim ama
    indirimli fiyat zam öncesinin altına inmiyor."""
    cp = _compress([(_ts(t), float(p)) for t, p in points if float(p) > 0])
    events = []
    for j in range(2, len(cp)):
        P, D = cp[j - 1][1], cp[j][1]
        if D > P * 0.85:
            continue
        k = j - 1
        while k - 1 >= 0 and cp[k - 1][1] >= P * 0.98:
            k -= 1
        if k == 0:
            continue
        R = cp[k - 1][1]
        days_high = (cp[j][0] - cp[k][0]).total_seconds() / 86400
        days_low = (cp[k][0] - cp[k - 1][0]).total_seconds() / 86400
        if P >= R * 1.15 and D >= R * 0.99 and 3 <= days_high <= 60 and days_low >= 7:
            events.append(PumpEvent(cp[j][0], R, P, D, days_high))
    return events


# ── FırsatVakti Puanı (0–100) ─────────────────────────────────────────────────
# Yeni tema (2026-10-13): ana sayfa akışı bu puana göre sıralanır. Ağırlıklar:
#   40 — 30 günlük (süre ağırlıklı) ortalamaya göre indirim (%40 indirim = tam puan)
#   25 — tüm zamanların en düşük fiyatı (yalnızca 30 günün en düşüğüyse 12)
#   20 — platformlar arası en ucuz (karşılaştırma grubu yoksa nötr 10)
#   15 — temiz geçmiş: indirimden önce zam yok ve aynı fiyat son 30 günde tekrarlanmamış
# "Önce zam sonra indirim" ayrıca 30 puan düşürür.
@dataclass
class DealScore:
    score: int
    ref_price: Optional[float]      # 30 günlük ortalama (kartta üstü çizili fiyat)
    pct: float                      # ortalamaya göre indirim %
    all_time_low: bool
    low30: bool
    repeated: bool
    pumped: bool


def deal_score(points: Sequence[Point], price: float, at: datetime,
               cheapest: Optional[bool] = None) -> DealScore:
    pts = _norm(points)
    cp = _compress(pts)
    # Mevcut fiyatın başladığı an (aynı fiyattaki son kesintisiz dönem)
    start = at
    for t, p in reversed(cp):
        if abs(p - price) < 0.005:
            start = t
        else:
            break
    win = _window(pts, start, 30)
    segs = _segments(win, start) if win else []
    ref = _weighted_median(segs) if segs else None
    pct = (ref - price) / ref * 100 if ref else 0.0
    before = [p for t, p in pts if t < start]
    all_time_low = bool(before) and price <= min(before) * 1.005
    low30 = bool(segs) and price < min(p for p, _ in segs) * NEW_LOW_TOLERANCE
    repeated = any(abs(p - price) / price < 0.01 for t, p in cp
                   if start - timedelta(days=30) <= t < start)
    pumped = any(e.date >= at - timedelta(days=90) for e in find_pump_events(pts))

    s = 40 * min(max(pct, 0) / 40, 1)
    s += 25 if all_time_low else (12 if low30 else 0)
    s += 20 if cheapest else (10 if cheapest is None else 0)
    s += 15 if not (repeated or pumped) else 0
    if pumped:
        s -= 30
    return DealScore(int(round(max(0, min(100, s)))), ref, pct, all_time_low, low30, repeated, pumped)
