#!/usr/bin/env python3
"""
Hepsiburada DOM araştırma scripti.
Birden fazla ürün sayfasını açar, fiyat elementlerini ve JSON verilerini döker.
"""
import asyncio
import re
import json
import sys
from playwright.async_api import async_playwright

# Test edilecek URL'ler — farklı fiyat senaryolarını kapsasın
URLS = [
    # Normal fiyat
    "https://www.hepsiburada.com/samsung-galaxy-a55-5g-256-gb-akilli-telefon-pm-HB00000Q6F3T",
    # Sepette indirimli ürün (genellikle elektronik)
    "https://www.hepsiburada.com/apple-airpods-4-aktif-gurultu-onleme-kulaklik-pm-HB00000UJ23N",
    # Günlük tüketim / düşük fiyat
    "https://www.hepsiburada.com/sensodyne-hassasiyet-dis-macunu-75-ml-pm-HB0000006IPI",
]

STEALTH_SCRIPT = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
Object.defineProperty(navigator,'languages',{get:()=>['tr-TR','tr','en-US','en']});
"""

async def inspect_page(page, url: str, idx: int):
    print(f"\n{'='*70}")
    print(f"[{idx}] URL: {url}")
    print(f"{'='*70}")

    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(4000)

    # --- Gerçek URL (redirect kontrol) ---
    real_url = page.url
    print(f"  Final URL : {real_url}")

    # --- Sayfa başlığı ---
    title = await page.title()
    print(f"  Title     : {title}")

    # --- window.productState varlığı ---
    has_ps = await page.evaluate("() => typeof window.productState !== 'undefined'")
    print(f"  productState exists: {has_ps}")

    if has_ps:
        ps_raw = await page.evaluate("() => JSON.stringify(window.productState)")
        try:
            ps = json.loads(ps_raw)
            product = ps.get("product", {})
            print(f"  productState keys  : {list(ps.keys())}")
            print(f"  product keys       : {list(product.keys())[:20]}")

            # Fiyat alanları
            price_obj = product.get("price")
            print(f"  product.price      : {price_obj}")

            buybox = product.get("buyBoxInfo") or product.get("buyBox") or {}
            print(f"  buyBoxInfo keys    : {list(buybox.keys()) if isinstance(buybox, dict) else buybox}")

            price_info = buybox.get("priceInfo") if isinstance(buybox, dict) else None
            print(f"  buyBoxInfo.priceInfo: {price_info}")

            # Üst seviye fiyat alanları
            for k in ["finalPrice", "discountedPrice", "currentPrice", "salePrice", "listPrice", "sortPrice"]:
                v = product.get(k)
                if v is not None:
                    print(f"  product.{k:20s}: {v}")

        except Exception as e:
            print(f"  productState parse hatası: {e}")

    # --- __NEXT_DATA__ varlığı ---
    next_data_raw = await page.evaluate("""() => {
        const el = document.getElementById('__NEXT_DATA__');
        return el ? el.textContent.substring(0, 2000) : null;
    }""")
    if next_data_raw:
        print(f"\n  __NEXT_DATA__ ilk 500 char: {next_data_raw[:500]}")
    else:
        print(f"\n  __NEXT_DATA__: YOK")

    # --- JSON-LD fiyat ---
    ld_prices = await page.evaluate("""() => {
        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
        const results = [];
        scripts.forEach(s => {
            try {
                const j = JSON.parse(s.textContent);
                const items = Array.isArray(j) ? j : [j];
                items.forEach(item => {
                    if (item.offers) results.push({type: item['@type'], offers: item.offers});
                });
            } catch(e) {}
        });
        return results;
    }""")
    print(f"\n  JSON-LD offers: {json.dumps(ld_prices, ensure_ascii=False)[:400]}")

    # --- data-test-id elementleri ve içerikleri ---
    print(f"\n  --- data-test-id fiyat elementleri ---")
    price_test_ids = [
        "price", "default-price", "checkout-price", "prev-price",
        "see-earnings", "payment-options", "PremiumBanner",
        "merchant-coupons", "buybox-price", "price-value",
        "original-price", "campaign-price", "list-price",
    ]
    for tid in price_test_ids:
        el = await page.query_selector(f'[data-test-id="{tid}"]')
        if el:
            txt = (await el.inner_text()).strip().replace('\n', ' | ')[:120]
            print(f"    [{tid}] => {txt!r}")
        else:
            print(f"    [{tid}] => YOK")

    # --- TÜM data-test-id değerlerini listele (fiyat ile ilgili olabilecekler) ---
    print(f"\n  --- Sayfadaki TÜM data-test-id'ler (fiyat içerenler) ---")
    all_testids = await page.evaluate("""() => {
        const els = document.querySelectorAll('[data-test-id]');
        const results = [];
        els.forEach(el => {
            const tid = el.getAttribute('data-test-id');
            const txt = el.innerText?.trim().substring(0, 80) || '';
            if (txt.includes('TL') || txt.includes('₺') ||
                tid.toLowerCase().includes('price') ||
                tid.toLowerCase().includes('fiyat') ||
                tid.toLowerCase().includes('pay') ||
                tid.toLowerCase().includes('discount') ||
                tid.toLowerCase().includes('coupon') ||
                tid.toLowerCase().includes('premium')) {
                results.push({tid, txt: txt.replace(/\\n/g, ' | ')});
            }
        });
        return results;
    }""")
    for item in all_testids:
        print(f"    [{item['tid']}] => {item['txt']!r}")

    # --- itemprop="price" ---
    itemprop = await page.evaluate("""() => {
        const el = document.querySelector('[itemprop="price"]');
        return el ? {tag: el.tagName, content: el.getAttribute('content'), text: el.innerText} : null;
    }""")
    print(f"\n  itemprop=price: {itemprop}")

    # --- class içinde 'price' veya 'Price' geçen elementler (ilk 15) ---
    print(f"\n  --- class'ında 'price/Price' geçen elementler (fiyat içerenler, ilk 15) ---")
    class_prices = await page.evaluate("""() => {
        const all = document.querySelectorAll('[class*="price"], [class*="Price"]');
        const results = [];
        all.forEach(el => {
            const txt = el.innerText?.trim();
            if (txt && (txt.includes('TL') || txt.includes('₺')) && txt.length < 100) {
                const cls = el.className?.toString().substring(0, 60);
                results.push({tag: el.tagName, cls, txt: txt.replace(/\\n/g, ' | ')});
            }
        });
        return results.slice(0, 15);
    }""")
    for item in class_prices:
        print(f"    <{item['tag']} class={item['cls']!r}> => {item['txt']!r}")

    # --- HTML'de TL içeren script blokları (JSON fiyat) ---
    print(f"\n  --- HTML'deki fiyat pattern'leri (productState dışı script'ler) ---")
    html = await page.content()

    # window.* fiyat değişkenleri
    for pat in [
        r'window\.\w+\s*=\s*\{[^}]{0,200}(?:price|fiyat)[^}]{0,200}\}',
        r'"(?:finalPrice|discountedPrice|currentPrice|salePrice|listPrice)"\s*:\s*"?([\d\.,]+)"?',
    ]:
        matches = re.findall(pat, html, re.IGNORECASE)[:3]
        if matches:
            for m in matches:
                print(f"    REGEX [{pat[:40]}] => {str(m)[:150]}")

    # --- Sepette indirim / checkout-price tespiti ---
    print(f"\n  --- Sepet İndirim Tespiti ---")
    checkout_txt = None
    el = await page.query_selector('[data-test-id="checkout-price"]')
    if el:
        checkout_txt = (await el.inner_text()).strip()
        print(f"    checkout-price mevcut: {checkout_txt!r}")
    else:
        print(f"    checkout-price: YOK (normal fiyat)")

    # HTML'de sepet sözcükleri
    for kw in ["sepette", "checkout", "sepetindirim", "SEPETTE"]:
        if kw.lower() in html.lower():
            print(f"    HTML'de '{kw}' geçiyor: EVET")

    print(f"\n  {'─'*60}")
    print(f"  ÖZET: title={title[:60]!r}")
    print(f"        checkout_discount={'EVET' if el else 'HAYIR'}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_init_script(STEALTH_SCRIPT)

        for idx, url in enumerate(URLS, 1):
            page = await context.new_page()
            try:
                await inspect_page(page, url, idx)
            except Exception as e:
                print(f"  HATA: {e}")
            finally:
                await page.close()
            await asyncio.sleep(3)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
