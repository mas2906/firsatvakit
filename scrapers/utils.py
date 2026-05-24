#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tüm scraperlar için ortak yardımcı fonksiyonlar."""

import os
import re
import random
import asyncio
import logging
import time as _time
from typing import Optional

log = logging.getLogger("utils")

CLOUDFLARE_TITLES = ["Attention Required", "Just a moment", "Checking your browser"]

# Lazy init: modül import edildiğinde event loop olmayabilir
_PLAYWRIGHT_SEM = None

def get_playwright_sem() -> asyncio.Semaphore:
    global _PLAYWRIGHT_SEM
    if _PLAYWRIGHT_SEM is None:
        _PLAYWRIGHT_SEM = asyncio.Semaphore(20)
    return _PLAYWRIGHT_SEM



STEALTH_SCRIPT = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['tr-TR','tr','en-US','en']});
Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
Object.defineProperty(navigator,'permissions',{get:()=>({query:()=>Promise.resolve({state:'granted'})})});
Object.defineProperty(document,'hidden',{get:()=>false});
Object.defineProperty(document,'visibilityState',{get:()=>'visible'});

// Gerçekçi Chrome plugin listesi (eski [1,2,3,4,5] yerine)
(function(){
  const pd=[
    {name:'PDF Viewer',filename:'internal-pdf-viewer',description:'Portable Document Format'},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
    {name:'Chromium PDF Viewer',filename:'internal-pdf-viewer',description:''},
    {name:'Microsoft Edge PDF Viewer',filename:'internal-pdf-viewer',description:''},
    {name:'WebKit built-in PDF',filename:'internal-pdf-viewer',description:''}
  ];
  const arr=Object.assign([...pd],{item:i=>pd[i],namedItem:n=>pd.find(p=>p.name===n)||null,refresh:()=>{}});
  Object.defineProperty(navigator,'plugins',{get:()=>arr});
  Object.defineProperty(navigator,'mimeTypes',{get:()=>({length:2})});
})();

// WebGL vendor/renderer — havuzdan rastgele seç
(function(){
  const gpus=[
    ['Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ['Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Super Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ['Google Inc. (Intel)','ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ['Google Inc. (AMD)','ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ['Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
  ];
  const g=gpus[Math.floor(Math.random()*gpus.length)];
  function patch(ctx){
    const orig=ctx.prototype.getParameter;
    ctx.prototype.getParameter=function(p){
      if(p===37445) return g[0];
      if(p===37446) return g[1];
      return orig.call(this,p);
    };
  }
  try{patch(WebGLRenderingContext);}catch(e){}
  try{patch(WebGL2RenderingContext);}catch(e){}
})();

// Canvas parmak izi — geçici kopya üzerinde minimal gürültü ekle
(function(){
  const origToDataURL=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(type,q){
    if(this.width>16&&this.height>16){
      try{
        const tmp=document.createElement('canvas');
        tmp.width=this.width; tmp.height=this.height;
        const tc=tmp.getContext('2d');
        tc.drawImage(this,0,0);
        const id=tc.getImageData(0,0,tmp.width,tmp.height);
        const d=id.data;
        for(let i=0;i<d.length;i+=4){if(Math.random()<0.02)d[i]^=1;}
        tc.putImageData(id,0,0);
        return origToDataURL.apply(tmp,[type,q]);
      }catch(e){}
    }
    return origToDataURL.apply(this,arguments);
  };
})();

// screen boyutları viewport ile uyumlu
Object.defineProperty(screen,'colorDepth',{get:()=>24});
Object.defineProperty(screen,'pixelDepth',{get:()=>24});

// navigator.connection (Network Information API)
(function(){
  const conn={downlink:10,effectiveType:'4g',rtt:50,saveData:false,
    addEventListener:function(){},removeEventListener:function(){}};
  Object.defineProperty(navigator,'connection',{get:()=>conn});
})();

// outerWidth/Height viewport ile eşleşsin
Object.defineProperty(window,'outerWidth',{get:()=>window.innerWidth||1920});
Object.defineProperty(window,'outerHeight',{get:()=>(window.innerHeight||1080)+74});
"""

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
]

MOBILE_UA_POOL = [
    # iPhone - iOS Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    # iPhone - Chrome Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/136.0.7103.56 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/135.0.7049.83 Mobile/15E148 Safari/604.1",
    # Android - Chrome Mobile
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.60 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.111 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.60 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.135 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.111 Mobile Safari/537.36",
]

# (UA string → (viewport_w, viewport_h, device_scale_factor))
MOBILE_VIEWPORTS = [
    (390, 844, 3.0),   # iPhone 14
    (393, 852, 3.0),   # iPhone 15
    (375, 812, 3.0),   # iPhone X/11
    (412, 915, 2.625), # Pixel 7
    (360, 800, 3.0),   # Samsung Galaxy S21
    (414, 896, 2.0),   # iPhone 11 Pro Max
]

MOBILE_STEALTH_SCRIPT = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['tr-TR','tr','en-US','en']});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>6});
Object.defineProperty(navigator,'deviceMemory',{get:()=>4});
Object.defineProperty(document,'hidden',{get:()=>false});
Object.defineProperty(document,'visibilityState',{get:()=>'visible'});

(function(){
  const conn={downlink:20,effectiveType:'4g',rtt:30,saveData:false,
    addEventListener:function(){},removeEventListener:function(){}};
  Object.defineProperty(navigator,'connection',{get:()=>conn});
})();

Object.defineProperty(screen,'colorDepth',{get:()=>24});
Object.defineProperty(screen,'pixelDepth',{get:()=>24});
"""

DEFAULT_HEADERS = {
    "User-Agent": UA_POOL[0],
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def is_mobile_ua(ua: str) -> bool:
    return "Mobile" in ua or "Android" in ua or "iPhone" in ua


def get_stealth_headers(ua: str) -> dict:
    """UA string'inden Chrome sürümünü okur, eşleşen sec-ch-ua + Accept header'larını döner.
    Firefox/Safari UA'larında sec-ch-ua gönderilmez — boş dict döner.
    """
    mobile = is_mobile_ua(ua)

    if "Firefox" in ua or ("Safari" in ua and "Chrome" not in ua and "CriOS" not in ua):
        return {
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

    m = re.search(r"Chrome/(\d+)|CriOS/(\d+)", ua)
    v = (m.group(1) or m.group(2)) if m else "128"

    if "Edg/" in ua:
        brand = f'"Microsoft Edge";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"Windows"'
    elif "iPhone" in ua or ("Mobile" in ua and "Android" not in ua):
        brand = f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"iOS"'
    elif "Android" in ua:
        brand = f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"Android"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        brand = f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"macOS"'
    elif "X11" in ua or "Linux" in ua:
        brand = f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"Linux"'
    else:
        brand = f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not/A)Brand";v="8"'
        platform = '"Windows"'

    return {
        "sec-ch-ua": brand,
        "sec-ch-ua-mobile": "?1" if mobile else "?0",
        "sec-ch-ua-platform": platform,
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }


class RateLimiter:
    """Platform başına tek tip rate limiting — thread-safe asyncio semaphore."""

    def __init__(self, min_delay: float, max_delay: float):
        self._min = min_delay
        self._max = max_delay
        self._last_ts = 0.0
        self._sem = asyncio.Semaphore(1)

    async def wait(self):
        async with self._sem:
            elapsed = _time.monotonic() - self._last_ts
            delay = random.uniform(self._min, self._max) - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_ts = _time.monotonic()

    def reset(self):
        """Son istek zamanını sıfırla — sonraki wait() anında geçer."""
        self._last_ts = 0.0


def normalize_image_url(url: str | None) -> str | None:
    """Protokol-relative veya eksik URL'leri düzelt."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return None


CART_DISCOUNT_PATTERNS = [
    "sepette indirim",
    "sepete indirim",
    "sepette ekstra",
    "sepete ekstra",
    "2. ürüne",
    "2.ürüne",
    "ikinci ürüne",
    "çoklu alım",
    "n ürüne",
    "kombinasyon fiyat",
    "ek indirim sepet",
    "cart discount",
]


def detect_cart_discount(html_text: str) -> bool:
    """HTML metninde sepette indirim / yanıltıcı fiyat göstergesi tespit eder."""
    t = (html_text or "").lower()
    return any(p in t for p in CART_DISCOUNT_PATTERNS)


def parse_price_tr_clean(text: str) -> Optional[float]:
    """
    Türkçe ve standart fiyat formatlarını doğru parse eder.
    Yuvarlama YAPILMAZ — fiyat ne ise tam olarak döner.

      '49.999 TL'   → 49999.0   (binlik nokta)
      '1.299,99 TL' → 1299.99   (binlik nokta + ondalık virgül)
      '1.799,28 TL' → 1799.28
      '399,80 TL'   → 399.8     (Python float — matematiksel olarak 399.80 ile aynı)
      '49999.00'    → 49999.0   (ondalık nokta)
    """
    t = re.sub(r"[^\d,.]", "", (text or "").strip())
    if not t:
        return None

    if "," in t and "." in t:
        if t.rfind(".") > t.rfind(","):
            t = t.replace(",", "")           # ABD: 1,299.99
        else:
            t = t.replace(".", "").replace(",", ".")  # TR: 1.299,99
    elif "," in t:
        parts = t.split(",")
        if len(parts[-1]) <= 2:
            t = t.replace(",", ".")          # ondalık virgül: 399,80
        else:
            t = t.replace(",", "")           # binlik virgül: 1,299
    elif "." in t:
        parts = t.split(".")
        if len(parts[-1]) > 2:
            t = t.replace(".", "")           # binlik nokta: 49.999

    try:
        v = float(t)
        # 10 TL altı veya 999.999 TL üstü → muhtemelen yanlış parse
        return v if 0.5 < v < 1_000_000 else None
    except Exception:
        return None


# parse_price_tr eski adla da erişilebilsin (geriye dönük uyumluluk)
parse_price_tr = parse_price_tr_clean


def _json_complete(text: str, marker: str) -> bool:
    """True if JSON object starting at or after marker is fully present in text."""
    idx = text.find(marker)
    if idx == -1:
        return False
    brace = text.find('{', idx)
    if brace == -1:
        return False
    depth = 0
    in_str = False
    esc = False
    for ch in text[brace:]:
        if esc:
            esc = False
            continue
        if ch == '\\' and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return True
    return False


async def stream_fetch(
    session,
    url: str,
    json_markers: list = None,
    html_markers: list = None,
    max_kb: int = 600,
    timeout: int = 15,
    headers: dict = None,
    validate_fn=None,
) -> Optional[str]:
    """Stream HTTP response; stop when markers are satisfied AND validate_fn passes.

    json_markers : stop when any marker's JSON object (brace-balanced) is complete.
    html_markers : stop when ALL strings in the list are present.
    validate_fn  : callable(text)->bool — called after markers are met; if it
                   returns False, streaming continues until max_kb is reached.
    Falls back to a full (non-streaming) download if streaming is unavailable.
    """
    max_bytes = max_kb * 1024
    json_markers = json_markers or []
    html_markers = html_markers or []
    req_kwargs: dict = {"timeout": timeout}
    if headers:
        req_kwargs["headers"] = headers

    chunks: list = []
    total = 0
    found_json: set = set()
    complete_json: set = set()   # markers whose JSON object is fully downloaded
    last_check = 0
    _INTERVAL = 25_000           # re-run validate every 25 KB after markers complete

    try:
        r = await session.get(url, stream=True, **req_kwargs)
        if r.status_code != 200:
            log.info(f"[stream_fetch] HTTP {r.status_code} — {url[:60]}")
            try:
                await r.aclose()
            except Exception:
                pass
            # 404/410 kalıcı olarak yok — çağırana sinyal ver
            if r.status_code in (404, 410):
                return "__NOT_FOUND__"
            return None
        try:
            async for chunk in r.aiter_content():
                raw = chunk if isinstance(chunk, bytes) else chunk.encode()
                chunks.append(raw)
                total += len(raw)

                if total > 10_000 and total - last_check >= _INTERVAL:
                    last_check = total
                    text = b"".join(chunks).decode("utf-8", errors="ignore")

                    # Update which JSON markers have complete objects
                    for m in json_markers:
                        if m in text:
                            found_json.add(m)
                        if m in found_json and m not in complete_json:
                            if _json_complete(text, m):
                                complete_json.add(m)

                    # Check stopping condition
                    json_ready = bool(complete_json)
                    html_ready = bool(html_markers) and all(m in text for m in html_markers)
                    if json_ready or html_ready:
                        if validate_fn is None or validate_fn(text):
                            return text
                        # validate failed — keep streaming, will re-check next interval

                if total >= max_bytes:
                    break
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
        if total <= 5000:
            log.info(f"[stream_fetch] yanıt çok kısa ({total}B) — {url[:60]}")
        return b"".join(chunks).decode("utf-8", errors="ignore") if total > 5000 else None
    except asyncio.CancelledError:
        raise
    except Exception as _ex:
        log.info(f"[stream_fetch] streaming hatası: {_ex} — {url[:60]}")

    # Fallback: non-streaming full download
    try:
        r = await session.get(url, **req_kwargs)
        sz = len(r.text) if r.text else 0
        if r.status_code == 200 and sz > 5000:
            return r.text
        log.info(f"[stream_fetch] fallback HTTP {r.status_code} {sz}B — {url[:60]}")
    except asyncio.CancelledError:
        raise
    except Exception as _ex:
        log.info(f"[stream_fetch] fallback hatası: {_ex} — {url[:60]}")
    return None
