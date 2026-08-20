#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FırsatVakti — Otomatik SEO Makale Yazıcı
Günde 2 adet Türkçe, SEO uyumlu makale üretir ve veritabanına kaydeder.
Kullanım: python article_writer.py [--count 2] [--status draft|published]
"""

import os, sys, json, argparse
from datetime import datetime, date
from dotenv import load_dotenv
load_dotenv()

import anthropic
from db import get_db, init_db
from seo_content import create_article

# ── Ayarlar ──────────────────────────────────────────────────────────────────
MODEL        = "claude-haiku-4-5"   # Günlük makale için maliyet-etkin model
ADMIN_USER   = 1                    # Makaleleri hangi kullanıcı adına kaydet
DEFAULT_CNT  = 2                    # Günde kaç makale
# ─────────────────────────────────────────────────────────────────────────────

# Konu havuzu — her gün farklı konular seçilir
TOPIC_POOL = [
    # Alışveriş rehberleri
    ("Amazon Türkiye'de En İyi Fiyatı Nasıl Bulursunuz?",       "rehber",   "amazon,fiyat takibi,indirim"),
    ("Trendyol Flash Sale: Fırsatları Kaçırmamak İçin 7 İpucu", "rehber",   "trendyol,flash sale,indirim"),
    ("Hepsiburada Kuponları Nasıl Kullanılır? Tam Rehber",       "rehber",   "hepsiburada,kupon,tasarruf"),
    ("N11'de En Ucuz Ürünü Bulmanın Yolları",                    "rehber",   "n11,fiyat karşılaştırma"),
    ("Sepet İndirimi Nedir? Platforma Göre Farklar",             "rehber",   "sepet indirimi,kampanya"),
    ("Online Alışverişte Sahte İndirimleri Fark Etmenin 5 Yolu", "rehber",   "sahte indirim,tüketici rehberi"),
    ("Fiyat Takip Uygulamaları: Hangi Platform Daha İyi?",       "rehber",   "fiyat takibi,karşılaştırma"),
    ("Kredi Kartı Kampanyaları ile Ekstra İndirim Nasıl Yapılır","rehber",   "kredi kartı,kampanya,banka"),

    # Ürün kategorisi rehberleri
    ("Akıllı Telefon Alımında Fiyat-Performans Hesabı",          "rehber",   "akıllı telefon,fiyat performans"),
    ("Laptop Alırken Dikkat Edilmesi Gereken 10 Özellik",        "rehber",   "laptop,bilgisayar,teknik rehber"),
    ("En Uygun Fiyatlı Robot Süpürge: Marka Karşılaştırması",    "karşılaştırma", "robot süpürge,beyaz eşya"),
    ("Kulaklık Alırken Nelere Bakmalısınız?",                    "rehber",   "kulaklık,ses,elektronik"),
    ("Hava Fryer Mı Microwave Mı? Hangisi Alınmalı?",            "karşılaştırma", "mutfak,hava fryer,ev aleti"),
    ("Akıllı Saat Satın Alımında Dikkat Edilecekler",            "rehber",   "akıllı saat,giyilebilir teknoloji"),

    # Sezonluk ve dönemsel
    ("Yaz İndirimleri İçin Alışveriş Stratejisi",                "rehber",   "yaz indirimi,sezonluk,kampanya"),
    ("Kış Sezonu Öncesi Ev Eşyası Alımında En İyi Zaman",        "rehber",   "kış,ev eşyası,sezonluk fırsat"),
    ("Back to School: Okul Alışverişinde Tasarruf Rehberi",      "rehber",   "okul alışverişi,eğitim,indirim"),
    ("Anneler Günü Hediyesinde En İyi Fiyat Nasıl Bulunur?",     "rehber",   "hediye,anneler günü,tasarruf"),
    ("Babalar Günü Kampanyaları: Nerede, Ne Kadar?",              "indirim",  "babalar günü,hediye,kampanya"),
    ("Ramazan ve Bayram İndirimleri: Alışveriş Takvimi",         "rehber",   "ramazan,bayram,indirim takvimi"),

    # Bilgilendirici içerikler
    ("E-Ticaret'te İade Hakları: Bilmeniz Gereken Her Şey",      "rehber",   "iade,tüketici hakları,e-ticaret"),
    ("Amazon Prime Üyeliği Değer Mi? Avantajlar ve Dezavantajlar","rehber",  "amazon prime,üyelik,değerlendirme"),
    ("Trendyol Premium Nedir, Kazandırır mı?",                   "rehber",   "trendyol premium,üyelik"),
    ("Kargo Ücretsizliğinin Sırrı: Eşik Değerler ve Taktikler", "rehber",   "kargo,ücretsiz teslimat,taktik"),
    ("Garanti Belgesi ve Teknik Servis: Hangi Markalar Daha İyi?","rehber",  "garanti,teknik servis,marka"),

    # Marka rehberleri
    ("Xiaomi Türkiye: Fiyat-Performans Ürünleri İnceleme",       "indirim",  "xiaomi,fiyat performans,akıllı telefon"),
    ("Samsung Kampanyaları: En İyi İndirim Dönemleri",           "indirim",  "samsung,kampanya,elektronik"),
    ("Apple Ürünlerinde Türkiye Fiyatları Neden Bu Kadar Yüksek?","rehber",  "apple,iphone,fiyat analizi"),
]


def pick_topics(db, count: int) -> list[dict]:
    """Bugün için konu seç — daha önce yazılmış olanları atla."""
    used_titles = set(
        row[0] for row in db.execute(
            "SELECT title FROM articles WHERE created_at >= date('now', '-30 days')"
        ).fetchall()
    )
    available = [t for t in TOPIC_POOL if t[0] not in used_titles]
    if not available:
        available = TOPIC_POOL  # Hepsi kullanıldıysa sıfırla

    # Günün tarihi seed ile deterministik ama günlük değişen seçim.
    # NOT: Python'un yerleşik hash() fonksiyonu str için process başına
    # rastgele tuzlanır (PYTHONHASHSEED) — aynı gün ikinci kez çalıştırıldığında
    # farklı bir sıralama üretip "deterministik" iddiasını bozardı. md5 sabittir.
    import hashlib
    today_seed = int(hashlib.md5(str(date.today()).encode()).hexdigest(), 16)
    shuffled = sorted(available, key=lambda x: hashlib.md5((x[0] + str(today_seed)).encode()).hexdigest())
    return [{"title": t[0], "category": t[1], "tags": t[2]} for t in shuffled[:count]]


def build_prompt(topic: dict) -> str:
    return f"""Sen FırsatVakti.com için Türkçe SEO içerik yazarısın. FırsatVakti, Amazon, Trendyol, Hepsiburada ve N11 gibi Türk e-ticaret platformlarındaki fırsatları ve indirimleri takip eden bir sitedir.

Aşağıdaki konuda SEO uyumlu bir makale yaz:

**Konu:** {topic['title']}
**Kategori:** {topic['category']}
**Etiketler:** {topic['tags']}

KURALLAR:
- Dil: Türkçe, sade ve anlaşılır
- Uzunluk: 600-900 kelime
- Format: Sadece HTML (h2, h3, p, ul, ol, strong, em etiketleri kullan — başka HTML etiketi yok)
- H1 YAZMA (site zaten koyuyor)
- En az 3 adet H2 başlığı olsun
- Anahtar kelimeler doğal geçsin, keyword stuffing yapma
- Gerçek ve faydalı bilgi ver
- FırsatVakti'yi doğal şekilde 1-2 kez referans ver ("FırsatVakti'de fiyat takip edebilirsiniz" gibi)
- Özet (summary) için ayrı bir JSON alanı ekle

ÇIKTI FORMATI (JSON):
{{
  "summary": "160 karakteri geçmeyen SEO meta açıklaması",
  "body_html": "<h2>...</h2><p>...</p>..."
}}

Sadece bu JSON'u döndür, başka açıklama ekleme."""


def generate_article(client: anthropic.Anthropic, topic: dict) -> dict | None:
    print(f"  📝 Yazılıyor: {topic['title'][:60]}...")
    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": build_prompt(topic)}]
        ) as stream:
            raw = stream.get_final_message()

        text = next((b.text for b in raw.content if b.type == "text"), "")

        # JSON bloğunu çıkar
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        data = json.loads(text)
        return {
            "title":    topic["title"],
            "summary":  data.get("summary", "")[:160],
            "body_html": data.get("body_html", ""),
            "category": topic["category"],
            "tags":     topic["tags"],
        }
    except Exception as e:
        print(f"  ❌ Hata: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count",  type=int, default=DEFAULT_CNT, help="Kaç makale yazılsın")
    parser.add_argument("--status", default="published", choices=["draft", "published"])
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY tanımlı değil. .env dosyasına ekleyin.")
        sys.exit(1)

    init_db()
    db    = get_db()
    ai    = anthropic.Anthropic(api_key=api_key)
    topics = pick_topics(db, args.count)

    print(f"\n🤖 FırsatVakti Makale Yazıcı — {date.today()} — {len(topics)} makale\n")

    ok = 0
    for topic in topics:
        article = generate_article(ai, topic)
        if not article:
            continue

        try:
            result = create_article(
                db,
                title       = article["title"],
                body_html   = article["body_html"],
                author_id   = ADMIN_USER,
                category    = article["category"],
                tags        = article["tags"],
                summary     = article["summary"],
                status      = args.status,
            )
        except Exception as e:
            # Tek bir DB hatası (kilit, dup-slug, statement timeout) günün
            # geri kalan makalelerinin hiç yazılmamasına yol açmasın —
            # logla, bu konuyu atla, döngüye devam et.
            print(f"  ❌ DB hatası (konu atlanıyor): {e}")
            continue
        if result.get("ok"):
            ok += 1
            slug = result.get("slug", "")
            print(f"  ✅ Kaydedildi: /blog/{slug} (ID: {result.get('article_id')})")
        else:
            print(f"  ❌ DB hatası: {result}")

    print(f"\n✅ Tamamlandı: {ok}/{len(topics)} makale yazıldı\n")


if __name__ == "__main__":
    main()
