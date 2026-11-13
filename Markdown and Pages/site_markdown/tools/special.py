#!/usr/bin/env python3
"""Handlers for the sites that are not the standard GoIsrael SPA:
goisrael.com.br, i-israel.co.il, brochures.goisrael.com, israel.travel/kr.
Writes out/<key>.json in the same format as crawl.py."""
import asyncio, io, json, re, sys, time
import requests, pdfplumber
from playwright.async_api import async_playwright

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
TODAY = time.strftime("%Y-%m-%d")

IMG_JS = r"""
(sel) => {
  const scope = sel ? document.querySelector(sel) : (document.querySelector('#root') || document.body);
  const EXCL = 'header,nav,footer,[class*=cookie],[id*=cookie],.slick-cloned,.wt-cli-cookie-bar-container';
  const imgs = new Set();
  const add = u => { if (!u) return; u = u.trim().replace(/^url\(["']?/, '').replace(/["']?\)$/, ''); if (!u || u.startsWith('data:') || u==='none') return; try { imgs.add(new URL(u, location.href).href); } catch (e) {} };
  const best = s => { if (!s) return null; const c = s.split(',').map(x => x.trim().split(/\s+/)); c.sort((a,b)=>(parseFloat(b[1])||0)-(parseFloat(a[1])||0)); return c[0] && c[0][0]; };
  scope.querySelectorAll('*').forEach(el => {
    if (el.closest(EXCL)) return;
    if (el.tagName === 'IMG') { add(el.getAttribute('src')); add(best(el.getAttribute('srcset'))); add(el.getAttribute('data-src')); add(best(el.getAttribute('data-srcset'))); add(el.getAttribute('data-lazy-src')); }
    else if (el.tagName === 'SOURCE') add(best(el.getAttribute('srcset')));
    const bg = getComputedStyle(el).backgroundImage; if (bg && bg !== 'none') (bg.match(/url\([^)]+\)/g)||[]).forEach(add);
    const st = el.getAttribute('style'); if (st) (st.match(/url\([^)]+\)/g)||[]).forEach(add);
  });
  return [...imgs];
}
"""

# blocks from a DOM subtree, hidden content (accordions etc.) revealed
BLOCKS_JS = r"""
(args) => {
  const [sel, dropSel] = args;
  const src = document.querySelector(sel); const root = src.cloneNode(true); root.id = '__clone';
  root.querySelectorAll('script,style,noscript,iframe,svg,header,nav,footer,form,select,button,.slick-cloned,[class*=cookie],[id*=cookie]' + (dropSel ? ',' + dropSel : '')).forEach(e => e.remove());
  root.querySelectorAll('p br,h1 br,h2 br,h3 br,h4 br,li br').forEach(b => b.replaceWith(' '));
  const host = document.createElement('div'); host.style.cssText = 'position:absolute;left:-99999px;top:0;width:1200px';
  const st = document.createElement('style'); st.textContent = '#__clone,#__clone *{display:revert !important;visibility:visible !important;max-height:none !important;height:auto !important;opacity:1 !important;overflow:visible !important;position:static !important}';
  host.appendChild(st); host.appendChild(root); document.body.appendChild(host);
  const hm = {}; root.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => { const t = h.innerText.replace(/\s+/g,' ').trim(); if (t && !hm[t]) hm[t] = +h.tagName[1]; });
  const lis = new Set([...root.querySelectorAll('li')].map(l => l.innerText.replace(/\s+/g,' ').trim()));
  const out = root.innerText.split('\n').map(s => s.replace(/\s+/g,' ').trim()).filter(Boolean).map(t => hm[t] ? {t:'h', l:hm[t], x:t} : lis.has(t) ? {t:'li', x:t} : {t:'p', x:t});
  host.remove(); return out;
}
"""

def save(key, origin, pages, skipped, crawled, note=""):
    out = {"site": key, "origin": origin, "crawled": crawled, "generated": TODAY, "note": note,
           "pages": pages, "skipped": skipped}
    json.dump(out, open(f"out/{key}.json", "w"), ensure_ascii=False, indent=1)
    print(f"{key}: {len(pages)} pages, {len(skipped)} skipped, {sum(len(p['images']) for p in pages)} image refs")

async def goisrael_br(b):
    origin = "https://goisrael.com.br"
    pg = await b.new_page()
    await pg.goto(origin + "/", wait_until="load", timeout=90000)
    await pg.wait_for_timeout(2000)
    blocks = await pg.evaluate(BLOCKS_JS, ["#mtpl_dyan_block-main-content", ""])
    seen, bl = set(), []
    for x in blocks:
        if x["x"] in ("LEIA MAIS", "VEJA MAIS"): continue
        if len(x["x"]) > 30 and x["x"] in seen: continue
        seen.add(x["x"]); bl.append(x)
    imgs = await pg.evaluate(IMG_JS, "#mtpl_dyan_block-main-content")
    links = await pg.evaluate("[...new Set([...document.querySelectorAll('a[href]')].map(a=>a.href.split('#')[0]).filter(h=>{try{const u=new URL(h);return u.hostname.replace(/^www\\./,'')==='goisrael.com.br' && !/\\.(jpe?g|png|gif|webp|pdf|mp4)$/i.test(u.pathname) && u.pathname!=='/'}catch(e){return false}}))]")
    await pg.close()
    skipped = []
    for l in links:
        try:
            s = requests.get(l, headers=UA, timeout=20).status_code
        except Exception as e:
            s = str(e)[:60]
        skipped.append({"url": l, "reason": f"internal link returns HTTP {s}"})
    pages = [{"url": origin + "/", "title": "Visit Israel – Página inicial", "blocks": bl, "images": imgs, "duplicates": []}]
    save("goisrael.com.br", origin, pages, skipped, 1 + len(links),
         "Only the homepage is served; every internal link returns an error.")

async def i_israel(b):
    origin = "https://i-israel.co.il"
    pg = await b.new_page()
    await pg.goto(origin + "/", wait_until="load", timeout=90000)
    await pg.wait_for_timeout(2500)
    MENU = {"ראשי", "מבצעים לאזרחים ותיקים", "סיורים בחינם", "חופשה עירונית", "איפה תרצו לטייל?"}
    NOISE = re.compile(r"^(\(opens in a new tab\)|Previous|Next|הזמן עכשיו|חפשו לפי אזור)$")
    async def go(path):
        await pg.evaluate("p => { history.pushState({}, '', p); window.dispatchEvent(new PopStateEvent('popstate')); }", path)
        prev, st = "", 0
        await pg.wait_for_timeout(1200)
        for _ in range(10):
            c = await pg.evaluate("document.querySelector('#root').innerText")
            if c == prev:
                st += 1
                if st >= 2: break
            else: st = 0
            prev = c
            await pg.wait_for_timeout(600)
    # home: structured tables for hotels + guides
    hotels = await pg.evaluate("""[...document.querySelectorAll('#root .hotel-card')].filter(e=>!e.closest('.slick-cloned')).map(e=>{const g=c=>e.querySelector('.'+c)?.textContent.trim()||''; const site=[...e.querySelectorAll('a')].map(a=>a.getAttribute('href')).find(h=>h&&/^https?:/.test(h))||''; return [g('ac-title'),g('ac-type'),g('ac-loctext'),g('ac-tel'),site];})""")
    guides = await pg.evaluate("""[...document.querySelectorAll('#root .siur-data')].map(e=>{const leaf=[...e.querySelectorAll('*')].filter(x=>x.children.length===0&&x.textContent.trim()).map(x=>x.textContent.trim()); return [e.querySelector('.ac-name')?.textContent.trim()||leaf[0], leaf[1]||'', leaf[2]||'', e.querySelector('.ac-languages')?.textContent.trim()||'', e.querySelector('.ac-places')?.textContent.trim()||''];})""")
    home_blocks = await pg.evaluate(BLOCKS_JS, ["#root", ".hotel-card,.siur-data"])
    home_imgs = await pg.evaluate(IMG_JS, None)
    hb = [x for x in home_blocks if x["x"] not in MENU and not NOISE.match(x["x"])]
    a = next((i for i, x in enumerate(hb) if x["x"] == "בחרו אזורים:"), -1)
    z = next((i for i, x in enumerate(hb) if x["x"] == "אנגלית"), -1)
    if 0 <= a < z: del hb[a:z + 1]
    # insert tables after their section markers
    def insert_after(text, block):
        i = next((i for i, x in enumerate(hb) if x.get("x","").startswith(text)), None)
        if i is not None: hb.insert(i + 1, block)
        else: hb.append(block)
    insert_after("צאו לחופשה ב 20% הנחה", {"t": "table", "head": ["שם", "סוג", "מיקום", "טלפון", "אתר"], "rows": hotels})
    insert_after("מורי דרך לסיורים באזור", {"t": "table", "head": ["שם מורה הדרך", "טלפון", "דוא\"ל", "שפה", "אזורים"], "rows": guides})
    h1 = next((x["x"] for x in hb if x["t"] == "h" and x["l"] == 1), "דף הבית")
    pages = [{"url": origin + "/", "title": h1, "blocks": hb, "images": home_imgs, "duplicates": []}]
    posts = []
    for n in range(1, 8):
        await go(f"/post/{n}/")
        bl = await pg.evaluate(BLOCKS_JS, ["#root", ""])
        im = await pg.evaluate(IMG_JS, None)
        posts.append((n, bl, im))
    titles = [next((x["x"] for x in bl if x["t"] == "h" and x["l"] == 1), "") for _, bl, _ in posts]
    for (n, bl, im), title in zip(posts, titles):
        bl = [x for x in bl if x["x"] not in MENU and not NOISE.match(x["x"])]
        ti = next(i for i, x in enumerate(bl) if x["t"] == "h" and x["l"] == 1)
        body = bl[ti:]
        cut = next((i for i, x in enumerate(body) if i > 3 and x["x"] in titles and x["x"] != title), None)
        if cut: body = body[:cut - 1]
        pages.append({"url": f"{origin}/post/{n}/", "title": title, "blocks": body, "images": im, "duplicates": []})
    await pg.close()
    save("i-israel.co.il", origin, pages, [], 8,
         "Single-page app: the 7 posts render only through in-app navigation; opening them directly returns HTTP 404.")

def pdf_blocks(url):
    data = requests.get(url, headers=UA, timeout=120).content
    blocks = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, p in enumerate(pdf.pages, 1):
            txt = p.extract_text() or ""
            lines = [l.strip() for l in txt.split("\n")]
            paras, cur = [], ""
            for l in lines:
                if not l:
                    if cur: paras.append(cur); cur = ""
                    continue
                if cur and not re.search(r"[.!?:;”\"»)]$", cur) and (l[:1].islower() or cur.endswith((",", "-", "–"))):
                    cur = (cur[:-1] if cur.endswith("-") else cur + " ") + l
                else:
                    if cur: paras.append(cur)
                    cur = l
            if cur: paras.append(cur)
            paras = [x for x in paras if not re.fullmatch(r"\d{1,3}", x)]
            if paras:
                blocks.append({"t": "h", "l": 2, "x": f"Page {i}"})
                blocks += [{"t": "p", "x": x} for x in paras]
    return blocks, len(pdf.pages)

async def brochures(b):
    origin = "https://brochures.goisrael.com"
    pages, skipped = [], []
    pg = await b.new_page()
    NOISE = re.compile(r"^(VIEW NOW|DOWNLOAD|JETZT ANZEIGEN|HERUNTERLADEN|VER AHORA|DESCARGAR|CONSULTER|TÉLÉCHARGER|VISUALIZZA ORA|SCARICA)$")
    for lang, label in [("/", "EN"), ("/fr/", "FR"), ("/it/", "IT"), ("/es/", "ES"), ("/de/", "DE")]:
        await pg.goto(origin + lang, wait_until="load", timeout=90000)
        await pg.wait_for_timeout(1500)
        bl = await pg.evaluate(BLOCKS_JS, ["body", ".cookie,#cookie"])
        bl = [x for x in bl if not NOISE.match(x["x"]) and not x["x"].startswith(("We use cookies", "Accept cookies"))]
        im = await pg.evaluate(IMG_JS, "body")
        im = [u for u in im if "EuropeanCookies" not in u]
        pages.append({"url": origin + lang, "title": f"Brochures – landing page ({label})", "blocks": bl, "images": im, "duplicates": []})
    await pg.close()
    books = [
        ("Israel General Brochure – Miraculous, Passionate, Unique (EN)", "/brochures/miraculous_passionate_unique/", "/pdfs/miraculous_passionate_unique.pdf"),
        ("Holy Land – Biblical and Historical Sites (EN)", "/brochures/holy_land_brochure/", "/pdfs/holy_land_brochure.pdf"),
        ("Negev Desert Brochure (EN)", None, "/pdfs/Negev_booklet_pages.pdf"),
        ("Brochure Israël – Welcome to Israel (FR)", "/fr/brochures/welcome_to_israel/", "/fr/pdfs/welcome_to_israel.pdf"),
        ("Folleto General de Turismo (ES)", "/es/brochures/Tourism_General_booklet_Es/", "/es/pdfs/Tourism_General_booklet_Es.pdf"),
        ("Opuscolo Generale del Turismo (IT)", "/it/brochures/Tourism_General_booklet_It/", "/it/pdfs/Tourism_General_booklet_It.pdf"),
        ("Israel Broschüre – Wunderbar, Leidenschaftlich, Einzigartig (DE)", "/de/brochures/wunderbar_leidensehaftlieh_einzigartig/", "/de/pdfs/wunderbar_leidensehaftlieh_einzigartig.pdf"),
    ]
    for title, viewer, pdf in books:
        bl, n = pdf_blocks(origin + pdf)
        imgs = []
        if viewer:
            for pat in ("files/page/{}.jpg", "files/mobile/{}.jpg", "files/large/{}.jpg"):
                if requests.head(origin + viewer + pat.format(1), headers=UA, timeout=20).status_code == 200:
                    imgs = [origin + viewer + pat.format(i) for i in range(1, n + 1)]
                    break
        url = origin + (viewer or pdf)
        note = [{"t": "p", "x": f"PDF: {origin + pdf}"}]
        if not viewer:
            note.append({"t": "p", "x": "The online flipbook for this brochure (/brochures/negev_booklet_brochure/) returns HTTP 403, so the PDF is used as the page URL."})
        if "/it/" in pdf:
            note.append({"t": "p", "x": "Note: the download link on the IT landing page points to a non-existent file (/es/pdfs/Tourism_General_booklet_It.pdf); the PDF above is the working location."})
        pages.append({"url": url, "title": title, "blocks": note + bl, "images": imgs, "duplicates": []})
    skipped.append({"url": origin + "/brochures/negev_booklet_brochure/", "reason": "flipbook returns HTTP 403 – brochure included via its PDF instead"})
    save("brochures.goisrael.com", origin, pages, skipped, len(pages) + 1,
         "Brochure pages contain the text extracted from each brochure's PDF.")

def kr():
    r = requests.get("https://israel.travel/kr/", headers=UA, timeout=30, allow_redirects=True)
    save("israel.travel_kr", "https://israel.travel/kr", [],
         [{"url": "https://israel.travel/kr/", "reason": f"redirects to {r.url} (HTTP {r.status_code}) – no Korean content exists"}], 1,
         "The Korean section does not exist: /kr/ redirects to the English homepage.")

async def main(which):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        if which in ("all", "br"): await goisrael_br(b)
        if which in ("all", "ii"): await i_israel(b)
        if which in ("all", "bro"): await brochures(b)
        await b.close()
    if which in ("all", "kr"): kr()

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
