#!/usr/bin/env python3
"""Build <domain>.md and <domain>.sitemap.xml for every site from out/<key>.json.

* removes near-duplicate pages (invalid URLs that fall back to the homepage, or the
  same article served under two URLs) – text similarity >= 0.90 and similar images
* sitemap order = crawl (BFS discovery) order; the Markdown follows that order.
"""
import json, re, sys, os, difflib
from xml.sax.saxutils import escape

KEYS = ["www.goisrael.com", "goisrael.com.br", "israel.travel", "israel.travel_kr", "ru.goisrael.com", "de.goisrael.com",
        "fr.goisrael.com", "it.goisrael.com", "es.goisrael.com", "holyland.israel.travel", "i-israel.co.il",
        "agents.israel.travel", "brochures.goisrael.com"]

def text_of(p):
    return "\n".join(b.get("x", "") for b in p["blocks"] if b.get("t") != "table")

def near_dedupe(pages, skipped):
    kept = []
    for p in pages:
        t = text_of(p); imgs = set(p["images"])
        dup = None
        for k in kept:
            kt = k["_t"]
            if not t or not kt: continue
            is_home = k is kept[0]
            need = 0.90 if is_home else 0.98   # homepage fallbacks vary a little (weather, sliders)
            if min(len(t), len(kt)) / max(len(t), len(kt)) < (0.8 if is_home else need): continue
            sm = difflib.SequenceMatcher(None, t, kt, autojunk=False)
            if sm.real_quick_ratio() < need or sm.quick_ratio() < need: continue
            if sm.ratio() < need: continue
            ki = k["_i"]
            jac = len(imgs & ki) / max(1, len(imgs | ki))
            if jac >= 0.5:
                dup = k; break
        if dup:
            dup["duplicates"].append(p["url"])
            skipped.append({"url": p["url"], "reason": f"near-duplicate of {dup['url']} (same text and images; usually an invalid URL that falls back to another page)"})
        else:
            p["_t"], p["_i"] = t, imgs
            kept.append(p)
    for p in kept:
        p.pop("_t", None); p.pop("_i", None)
    return kept

NO_TEXT_NOTE = "> **Note:** the original page has no body text – it contains only a title and a video/images (checked against the live page)."

def md_escape_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")

COOKIE_NOISE = re.compile(r"^(To view this video, please accept|Чтобы просмотреть это видео|Um dieses Video anzusehen|Pour visionner cette vidéo|Per visualizzare questo video|Para ver este vídeo)|^(Cookie Settings|Настройки файлов cookie|Cookie-Einstellungen|Paramètres des cookies|Impostazioni dei cookie|Configuración de cookies)$")

def blocks_md(blocks):
    blocks = [b for b in blocks if b.get("t") == "table" or not COOKIE_NOISE.search(b.get("x", ""))]
    hs = [b["l"] for b in blocks if b.get("t") == "h"]
    base = min(hs) if hs else 1
    out = []
    for b in blocks:
        t = b.get("t")
        if t == "h":
            lvl = min(6, 4 + (b["l"] - base))
            out.append("#" * lvl + " " + b["x"].strip())
        elif t == "li":
            out.append("- " + b["x"].strip())
        elif t == "table":
            out.append("| " + " | ".join(md_escape_cell(h) for h in b["head"]) + " |")
            out.append("|" + "---|" * len(b["head"]))
            for r in b["rows"]:
                out.append("| " + " | ".join(md_escape_cell(c) for c in r) + " |")
            out.append("")
        else:
            out.append(b["x"].strip())
        out.append("")
    return "\n".join(out).strip()

def build(key):
    d = json.load(open(f"out/{key}.json"))
    for p in d["pages"]:
        p["title"] = re.sub(r"\s*[-–|«]\s*Visit Israel\s*$", "", p["title"]).strip() or p["title"]
    pages = d["pages"] if os.path.exists(f"out/{key}.sitemap.official.xml") else near_dedupe(d["pages"], d["skipped"])
    # sitemap
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          f"<!-- Generated {d['generated']} from a full rendered crawl of {d['origin']}/ – the site publishes no sitemap.xml -->",
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"  <url><loc>{escape(p['url'])}</loc></url>" for p in pages]
    sm.append("</urlset>")
    official = os.path.exists(f"out/{key}.sitemap.official.xml")
    if official:
        sm = open(f"out/{key}.sitemap.official.xml").read().rstrip("\n").split("\n")
    open(f"md/{key}.sitemap.xml", "w").write("\n".join(sm) + "\n")
    src = "the site's official sitemap (" + d["origin"] + "/sitemap_index.xml)" if official else "generated from a rendered crawl (the site has no sitemap.xml)"
    # markdown
    L = ["---", f"site: {key}", f"origin: {d['origin']}/", f"generated: {d['generated']}", f"page_count: {len(pages)}",
         f"image_count: {sum(len(p['images']) for p in pages)}", f"sitemap: {key}.sitemap.xml",
         f"sitemap_source: {src}", "---", "",
         f"# {key}", ""]
    if d.get("note"):
        L += [f"> {d['note']}", ""]
    L += [f"Pages: **{len(pages)}** · Crawled URLs: {d['crawled']} · Excluded URLs: {len(d['skipped'])} (listed at the end)", ""]
    for i, p in enumerate(pages, 1):
        L += [f"<!-- page {i} -->", f"## {i}. {p['title']}", "", f"- **URL:** {p['url']}"]
        if p.get("duplicates"):
            L.append("- **Same content also at:** " + ", ".join(p["duplicates"]))
        body = blocks_md(p["blocks"])
        if len(re.findall(r"\w+", body)) < 5:
            body = (body + "\n\n" if body else "") + NO_TEXT_NOTE
        L += ["", "### Content", "", body, "", f"### Images ({len(p['images'])})", ""]
        L += [f"{j}. {u}" for j, u in enumerate(p["images"], 1)] or ["_(no images)_"]
        L += ["", f"<!-- /page {i} -->", "", "---", ""]
    L += ["## Excluded URLs", "", "URLs that were found while crawling but are not separate pages:", ""]
    L += [f"- {s['url']} — {s['reason']}" for s in d["skipped"]] or ["_(none)_"]
    open(f"md/{key}.md", "w").write("\n".join(L) + "\n")
    print(f"{key:26} pages={len(pages):4}  excluded={len(d['skipped']):4}  images={sum(len(p['images']) for p in pages)}")

if __name__ == "__main__":
    import os; os.makedirs("md", exist_ok=True)
    for k in (sys.argv[1:] or KEYS):
        build(k)
