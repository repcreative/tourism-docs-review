#!/usr/bin/env python3
"""WordPress sites (same "dyan" theme): israel.travel and goisrael.com.br.

israel.travel   – uses the site's real sitemap (sitemap_index.xml), in its order.
goisrael.com.br – has no sitemap and its pretty permalinks all return 404, so the page
                  list is built from the WordPress REST API + link discovery, using the
                  query-string URLs that do work (?pagename=, ?video=, ?gallery=).
                  Blog posts cannot be opened as HTML at all; their content comes from
                  the REST API and their (broken) canonical URL is listed.
Writes out/<key>.json in the same format as crawl.py.
"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup, NavigableString, Comment

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
S = requests.Session(); S.headers.update(UA)
TODAY = time.strftime("%Y-%m-%d")
NOISE = re.compile(r"^(READ MORE|READ MORE >>|LEIA MAIS|VEJA MAIS|BOOK NOW|BOOK NOW >|RESERVE AGORA|Previous|Next|×|Watch Video|Assista ao vídeo)$", re.I)
DROP = "script,style,noscript,form,iframe,nav,footer,aside,.d-none,.maddon-deals-slide,.content-bottom-widgets,select,button,svg,.slick-cloned,[class*=cookie],[id*=cookie],.wt-cli-cookie-bar-container,#wp_access_helper_container"
BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "figcaption", "td", "th", "dt", "dd", "pre"}
IMG_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|avif)(\?|$)", re.I)

def best_srcset(s):
    if not s: return None
    c = [x.strip().split() for x in s.split(",") if x.strip()]
    c.sort(key=lambda a: float(re.sub(r"[^\d.]", "", a[1]) or 0) if len(a) > 1 else 0, reverse=True)
    return c[0][0] if c else None

WIDGETS = "aside,.d-none,.maddon-deals-slide,.content-bottom-widgets"

def extract(html, url, sel="#mtpl_dyan_block-main-content"):
    r = _extract(html, url, sel, DROP)
    if not r["blocks"]:
        # pages whose content *is* the deals widget (e.g. /deals/) – keep the widget
        r2 = _extract(html, url, sel, ",".join(x for x in DROP.split(",") if x not in WIDGETS.split(",")))
        if len(r2["blocks"]) > len(r["blocks"]): r = r2
    return r

def _extract(html, url, sel, drop):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(sel) or soup.select_one("main") or soup.select_one("article") or soup.body
    for el in root.select(drop): el.decompose()
    # images
    imgs = []
    def add(u):
        if not u: return
        u = u.strip().strip("'\"")
        if not u or u.startswith("data:"): return
        u = urljoin(url, u)
        if u not in imgs: imgs.append(u)
    for el in root.find_all(True):
        if el.name == "img":
            for a in ("src", "data-src", "data-lazy-src", "data-original"): add(el.get(a))
            add(best_srcset(el.get("srcset"))); add(best_srcset(el.get("data-srcset")))
        elif el.name == "source":
            add(best_srcset(el.get("srcset") or el.get("data-srcset")))
        elif el.name == "a" and el.get("href") and IMG_EXT.search(el["href"]):
            add(el["href"])
        for a in ("style", "data-bg", "data-background", "data-src"):
            v = el.get(a)
            if v and el.name != "img":
                for m in re.findall(r"url\(([^)]+)\)", v): add(m)
                if a != "style" and IMG_EXT.search(v): add(v)
    # text blocks in document order
    blocks = []
    def txt(el): return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    def walk(el):
        for c in el.children:
            if isinstance(c, Comment): continue
            if isinstance(c, NavigableString):
                t = re.sub(r"\s+", " ", str(c)).strip()
                if t and el.name not in BLOCK: blocks.append({"t": "p", "x": t})
                continue
            if not getattr(c, "name", None): continue
            if c.name in BLOCK:
                t = txt(c)
                if not t: continue
                if c.name[0] == "h" and len(c.name) == 2: blocks.append({"t": "h", "l": int(c.name[1]), "x": t})
                elif c.name == "li": blocks.append({"t": "li", "x": t})
                else: blocks.append({"t": "p", "x": t})
            elif c.name == "table":
                rows = [[txt(td) for td in tr.find_all(["td", "th"])] for tr in c.find_all("tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    w = max(len(r) for r in rows); rows = [r + [""] * (w - len(r)) for r in rows]
                    blocks.append({"t": "table", "head": rows[0], "rows": rows[1:]})
            elif c.name == "br":
                continue
            else:
                walk(c)
    walk(root)
    out, seen = [], set()
    for b in blocks:
        if b["t"] != "table":
            if NOISE.match(b["x"]): continue
            if len(b["x"]) > 40 and b["x"] in seen: continue   # carousel/slider repeats
            seen.add(b["x"])
            if out and out[-1].get("x") == b["x"]: continue
        out.append(b)
    h1 = next((b["x"] for b in out if b["t"] == "h" and b["l"] == 1), None)
    if not h1:
        m = soup.find("meta", property="og:title")
        h1 = (m.get("content") if m else None) or (soup.title.get_text() if soup.title else url)
        h1 = re.sub(r"\s*«.*$", "", h1).strip()
    return {"title": h1, "blocks": out, "images": imgs}

def fetch(url):
    try:
        r = S.get(url, timeout=60, allow_redirects=True)
        return r.status_code, r.url, r.text
    except Exception as e:
        return None, url, str(e)

def save(key, origin, pages, skipped, crawled, note):
    out = {"site": key, "origin": origin, "crawled": crawled, "generated": TODAY, "note": note, "pages": pages, "skipped": skipped}
    json.dump(out, open(f"out/{key}.json", "w"), ensure_ascii=False, indent=1)
    print(f"{key}: {len(pages)} pages, {len(skipped)} excluded, {sum(len(p['images']) for p in pages)} image refs")

# ---------------------------------------------------------------- israel.travel
def israel_travel():
    origin = "https://israel.travel"
    idx = S.get(origin + "/sitemap_index.xml", timeout=60).text
    urls = []
    for sm in re.findall(r"<loc>([^<]+)</loc>", idx):
        for u in re.findall(r"<loc>([^<]+)</loc>", S.get(sm, timeout=60).text):
            if not IMG_EXT.search(u) and u not in urls: urls.append(u)
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(fetch, urls))
    pages, skipped = [], []
    for u, (st, final, html) in zip(urls, res):
        if st != 200:
            # still listed (the sitemap says it exists) – content empty, validator will flag it
            skipped.append({"url": u, "reason": f"listed in sitemap but returns HTTP {st} (final URL {final})"})
            pages.append({"url": u, "title": "(page does not load)", "blocks": [{"t": "p", "x": f"This sitemap URL returns HTTP {st} – no content could be read."}], "images": [], "duplicates": [], "http_error": st})
            continue
        e = extract(html, final)
        pages.append({"url": u, "title": e["title"], "blocks": e["blocks"], "images": e["images"], "duplicates": []})
    save("israel.travel", origin, pages, [s for s in skipped], len(urls),
         "Pages follow the site's own sitemap (https://israel.travel/sitemap_index.xml) in its order. Note: israel.travel/kr/ (the Korean link) redirects to this English site.")
    # keep the published sitemap as the reference file
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          f"<!-- Official sitemap of {origin}/ (flattened from sitemap_index.xml on {TODAY}) -->",
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'] + [f"  <url><loc>{u}</loc></url>" for u in urls] + ["</urlset>"]
    open("out/israel.travel.sitemap.official.xml", "w").write("\n".join(sm) + "\n")

# ---------------------------------------------------------------- goisrael.com.br
def rest(path):
    items, page = [], 1
    while True:
        r = S.get(f"https://goisrael.com.br/?rest_route={path}&per_page=100&page={page}", timeout=60)
        t = r.text; i = t.find("[")
        try: d = json.loads(t[i:])
        except Exception: break
        if not isinstance(d, list) or not d: break
        items += d
        if page >= int(r.headers.get("X-WP-TotalPages", "1")): break
        page += 1
    return items

def goisrael_br():
    origin = "https://goisrael.com.br"
    wp_pages = {p["slug"]: p for p in rest("/wp/v2/pages") if p.get("status") == "publish"}
    posts = rest("/wp/v2/posts")
    post_slugs = {p["slug"] for p in posts}
    home_slug = next((s for s, p in wp_pages.items() if p["link"].rstrip("/") == origin), None)

    def working(pretty):
        """pretty permalink -> query-string URL that WordPress actually serves (or None)."""
        u = urlparse(pretty)
        if u.netloc.replace("www.", "") != "goisrael.com.br": return None
        parts = [p for p in u.path.split("/") if p]
        if not parts: return origin + "/"
        if parts[-1] in ("feed", "embed") or parts[0].startswith("wp-") or IMG_EXT.search(u.path): return None
        if parts[0] in ("video", "gallery") and len(parts) == 2: return f"{origin}/?{parts[0]}={parts[1]}"
        if parts[0] == "category": return "CATEGORY:" + "/".join(parts[1:])
        if len(parts) == 1 and parts[0] in post_slugs: return "POST:" + parts[0]
        if len(parts) == 1: return f"{origin}/?pagename={parts[0]}"
        return None

    order, seen, canon = [], set(), {}
    def push(pretty):
        w = working(pretty)
        if w and w not in seen:
            seen.add(w); order.append(w); canon[w] = pretty
    push(origin + "/")
    for s, p in wp_pages.items():
        if s != home_slug: push(p["link"])
    i = 0
    pages, skipped = {}, []
    while i < len(order):
        w = order[i]; i += 1
        if w.startswith(("POST:", "CATEGORY:")): continue
        st, final, html = fetch(w)
        if st != 200:
            pages[w] = None; skipped.append({"url": w, "reason": f"HTTP {st}"}); continue
        pages[w] = extract(html, final)
        for h in re.findall(r'href="([^"#]+)"', html):
            h = urljoin(origin + "/", h)
            if "?" in h: continue
            push(h)
    for p in posts:  # make sure every REST post is listed even if nothing links to it
        w = "POST:" + p["slug"]
        if w not in seen: seen.add(w); order.append(w); canon[w] = p["link"]
    post_by = {p["slug"]: p for p in posts}
    out = []
    for w in order:
        if w.startswith("CATEGORY:"):
            skipped.append({"url": canon[w], "reason": "category archive (listing page) – returns 404 on the live site; its posts are included individually"}); continue
        if w.startswith("POST:"):
            p = post_by[w[5:]]
            e = extract("<div id='x'>" + p["content"]["rendered"] + "</div>", p["link"], "#x")
            title = BeautifulSoup(p["title"]["rendered"], "html.parser").get_text().strip()
            note = {"t": "p", "x": f"Blog post – the live URL returns HTTP 404 (WordPress redirects every working form to it); content taken from the site's REST API: {origin}/?rest_route=/wp/v2/posts/{p['id']}"}
            out.append({"url": p["link"], "title": title, "blocks": [note] + e["blocks"], "images": e["images"], "duplicates": []})
            continue
        e = pages.get(w)
        if not e: continue
        out.append({"url": w, "title": e["title"] if w != origin + "/" else "Visit Israel – Página inicial", "blocks": e["blocks"],
                    "images": e["images"], "duplicates": [], "canonical": canon.get(w)})
    save("goisrael.com.br", origin, out, skipped, len(order),
         "The site has no sitemap and its pretty links (e.g. /video/…/, /faq/) return 404. Pages are listed by the query-string URLs WordPress still serves (?pagename=, ?video=, ?gallery=); blog posts can only be read through the REST API, so their (broken) canonical URLs are listed with the REST source.")

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "it"): israel_travel()
    if which in ("all", "br"): goisrael_br()
