#!/usr/bin/env python3
"""Render-and-crawl the GoIsrael family of sites with Playwright.

Usage: python3 crawl.py <site_key>
Writes out/<site_key>.json with every crawled URL (in BFS discovery order),
its extracted content blocks, images and the dedupe decision.
"""
import asyncio, json, re, sys, time
from urllib.parse import urljoin, urlparse, unquote
from playwright.async_api import async_playwright

SITES = {
    "www.goisrael.com":        {"start": "https://www.goisrael.com/", "mode": "spa"},
    "ru.goisrael.com":         {"start": "https://ru.goisrael.com/", "mode": "spa"},
    "de.goisrael.com":         {"start": "https://de.goisrael.com/", "mode": "spa"},
    "fr.goisrael.com":         {"start": "https://fr.goisrael.com/", "mode": "spa"},
    "it.goisrael.com":         {"start": "https://it.goisrael.com/", "mode": "spa"},
    "es.goisrael.com":         {"start": "https://es.goisrael.com/", "mode": "spa"},
    "holyland.israel.travel":  {"start": "https://holyland.israel.travel/", "mode": "spa"},
    "agents.israel.travel":    {"start": "https://agents.israel.travel/", "mode": "spa"},
}

NOISE = re.compile(r"^(READ MORE >>|READ MORE|WATCH NOW|Previous|Next|UK|RU|DE|FR|IT|ES|EN|ALL PHOTOS >>|ALLE FOTOS >>|TOUTES LES PHOTOS >>|TUTTE LE FOTO >>|TODAS LAS FOTOS >>|×|ПРОЧИТАТЬ БОЛЬШЕ >>|WEITERLESEN >>|LIRE LA SUITE >>|LEGGI DI PIÙ >>|LEE MÁS >>|Cookie-Einstellungen|Paramètres des cookies|Impostazioni dei cookie|Configuración de cookies|Accept cookies|Functional cookies|Click here|CLICK HERE|SUBMIT|logo)$|^(Um dieses Video anzusehen|Pour visionner cette vidéo|Per visualizzare questo video|Para ver este vídeo|We use cookies to presonalize)")
BRK = r"^(RELATED (POSTS|VIDEOS|ITINERARIES)|ПОХОЖИЕ СТАТЬИ|ПОХОЖИЕ ВИДЕО|ÄHNLICHE POSTS|ÄHNLICHE VIDEOS|ÄHNLICHE ARTIKEL|ARTICLES SIMILAIRES|VIDÉOS LIÉES:?|ARTÍCULOS RELACIONADOS|VÍDEOS? RELACIONADOS?:?|ARTICOLI CORRELATI|VIDEO CORRELATI:?)$"

EXTRACT_JS = r"""
(BRK) => {
  const brk = new RegExp(BRK, 'i');
  const root = document.querySelector('#root') || document.body;
  const EXCL = '.menu-outer-wrapper,.Footer-outer-wrapper,header,nav,footer,script,style,noscript,iframe,[class*=cookie],[id*=cookie],.slick-cloned';
  // where does the "related" section start (live DOM)?
  const heads = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')];
  const brkEl = heads.find(h => brk.test(h.innerText.trim()) && !h.closest(EXCL));
  const inScope = el => !el.closest(EXCL) && !(brkEl && (brkEl.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
  // images
  const imgs = new Set();
  const add = u => { if (!u) return; u = u.trim().replace(/^url\(["']?/, '').replace(/["']?\)$/, ''); if (!u || u.startsWith('data:') || u === 'none') return; try { imgs.add(new URL(u, location.href).href); } catch (e) {} };
  const bestSrcset = s => { if (!s) return null; const c = s.split(',').map(x => x.trim().split(/\s+/)); c.sort((a, b) => (parseFloat(b[1]) || 0) - (parseFloat(a[1]) || 0)); return c[0] && c[0][0]; };
  root.querySelectorAll('*').forEach(el => {
    if (!inScope(el)) return;
    if (el.tagName === 'IMG') { add(el.currentSrc || el.getAttribute('src')); add(bestSrcset(el.getAttribute('srcset'))); add(el.getAttribute('data-src')); }
    else if (el.tagName === 'SOURCE') add(bestSrcset(el.getAttribute('srcset')));
    else if (el.tagName === 'VIDEO') add(el.getAttribute('poster'));
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== 'none') (bg.match(/url\([^)]+\)/g) || []).forEach(add);
  });
  // text blocks (clone, without excluded parts)
  const clone = root.cloneNode(true);
  clone.querySelectorAll(EXCL).forEach(e => e.remove());
  const host = document.createElement('div'); host.style.cssText = 'position:absolute;left:-9999px;width:1200px';
  host.appendChild(clone); document.body.appendChild(host);
  const hmap = {}; clone.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => { const t = h.innerText.trim(); if (t && !hmap[t]) hmap[t] = +h.tagName[1]; });
  const lis = new Set([...clone.querySelectorAll('li')].map(l => l.innerText.trim()));
  const blocks = clone.innerText.split('\n').map(s => s.trim()).filter(Boolean).map(t => hmap[t] ? {t:'h', l:hmap[t], x:t} : lis.has(t) ? {t:'li', x:t} : {t:'p', x:t});
  host.remove();
  const links = [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'));
  return {blocks, images: [...imgs], links, docTitle: document.title, finalUrl: location.href};
}
"""

def norm(href, origin):
    try:
        u = urlparse(urljoin(origin + "/", href))
    except Exception:
        return None
    if f"{u.scheme}://{u.netloc}" != origin:
        return None
    p = u.path or "/"
    if re.search(r"\.(jpe?g|png|gif|pdf|zip|mp4|svg|webp|docx?|xlsx?)$", p, re.I):
        return None
    if len(re.findall(r"/Posts/", p, re.I)) > 1 or re.search(r"/Pages/.*/Posts/", p, re.I) or re.match(r"^/international", p, re.I):
        return None
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return origin + p

async def render(ctx, url):
    page = await ctx.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = resp.status if resp else None
        if urlparse(page.url).netloc != urlparse(url).netloc:
            return {"error": f"redirects to external site {page.url}", "status": status}
        prev, stable = "", 0
        await page.wait_for_timeout(2000)
        for _ in range(25):
            cur = await page.evaluate("(document.querySelector('#root')||document.body).innerText")
            if len(cur) > 150 and cur == prev:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev = cur
            await page.wait_for_timeout(800)
        # let lazy images resolve
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)
        data = await page.evaluate(EXTRACT_JS, BRK)
        data["status"] = status
        return data
    except Exception as e:
        return {"error": str(e)[:200]}
    finally:
        await page.close()

async def crawl(key):
    cfg = SITES[key]
    origin = cfg["start"].rstrip("/")
    order = [origin + "/"]
    seen = {origin + "/"}
    results = {}
    idx = 0
    sem_n = 5
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        inflight = {}
        while idx < len(order) or inflight:
            while idx < len(order) and len(inflight) < sem_n:
                u = order[idx]; idx += 1
                inflight[u] = asyncio.create_task(render(ctx, u))
            done, _ = await asyncio.wait(inflight.values(), return_when=asyncio.FIRST_COMPLETED)
            for u, t in list(inflight.items()):
                if t in done:
                    r = t.result(); del inflight[u]
                    results[u] = r
                    for h in r.get("links", []) or []:
                        n = norm(h, origin)
                        if n and n not in seen:
                            seen.add(n); order.append(n)
            print(f"\r{key}: {len(results)}/{len(order)}", end="", file=sys.stderr, flush=True)
        await browser.close()
    print(file=sys.stderr)
    # dedupe in discovery order
    pages, skipped, keys = [], [], {}
    for u in order:
        r = results[u]
        if r.get("error"):
            skipped.append({"url": u, "reason": "not loadable: " + r["error"]}); continue
        bl = []
        for b in r["blocks"]:
            if b["t"] == "h" and re.match(BRK, b["x"], re.I):
                break
            if NOISE.search(b["x"]):
                continue
            bl.append(b)
        k = "\n".join(b["x"] for b in bl)
        if not k:
            skipped.append({"url": u, "reason": "no text content"}); continue
        if k in keys:
            keys[k]["duplicates"].append(u)
            skipped.append({"url": u, "reason": "duplicate of " + keys[k]["url"]}); continue
        path = unquote(urlparse(u).path)
        fh = next((b for b in bl if b["t"] == "h"), None)
        title = fh["x"] if fh else (r.get("docTitle") or path)
        page = {"url": u, "title": title, "blocks": bl, "images": r["images"], "duplicates": []}
        keys[k] = page; pages.append(page)
    out = {"site": key, "origin": origin, "crawled": len(order), "generated": time.strftime("%Y-%m-%d"),
           "pages": pages, "skipped": skipped}
    json.dump(out, open(f"out/{key}.json", "w"), ensure_ascii=False, indent=1)
    print(f"{key}: {len(pages)} pages, {len(skipped)} skipped, {sum(len(p['images']) for p in pages)} image refs")

if __name__ == "__main__":
    asyncio.run(crawl(sys.argv[1]))
