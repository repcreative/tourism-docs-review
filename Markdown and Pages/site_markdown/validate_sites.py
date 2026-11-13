#!/usr/bin/env python3
"""Validate the per-site Markdown exports against their sitemaps.

For every <domain>.md (with <domain>.sitemap.xml next to it) it checks:
  1. structure  – valid front matter, every page has a number, title, URL and content,
                  image lists are well-formed and the counts in the header are right
  2. sitemap    – every sitemap URL is in the Markdown, nothing extra, same order
  3. HTTP       – every page URL and every image URL loads (status 200; images must be image/*)

Usage:  python3 validate_sites.py [md_dir] [--no-http] [--workers N]
Writes validation_report.json and validation_report.md into md_dir.
Exit code 0 = all structural and sitemap checks passed (HTTP findings are reported separately).
"""
import json, re, sys, os, time, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
REQUIRED_FM = ["site", "origin", "generated", "page_count", "image_count", "sitemap"]
PAGE_RE = re.compile(r"^<!-- page (\d+) -->\n(.*?)\n<!-- /page \1 -->$", re.S | re.M)

def parse_md(path):
    text = open(path, encoding="utf-8").read()
    errors, pages = [], []
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if not m:
        errors.append("missing YAML front matter")
    else:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1); fm[k.strip()] = v.strip()
        for k in REQUIRED_FM:
            if k not in fm: errors.append(f"front matter missing '{k}'")
    opens = len(re.findall(r"^<!-- page \d+ -->$", text, re.M))
    closes = len(re.findall(r"^<!-- /page \d+ -->$", text, re.M))
    if opens != closes:
        errors.append(f"unbalanced page markers ({opens} open / {closes} close)")
    for mm in PAGE_RE.finditer(text):
        n, body = int(mm.group(1)), mm.group(2)
        p = {"n": n, "errors": []}
        h = re.search(r"^## (\d+)\. (.+)$", body, re.M)
        if not h: p["errors"].append("missing '## N. Title' heading")
        else:
            if int(h.group(1)) != n: p["errors"].append(f"heading number {h.group(1)} != marker {n}")
            p["title"] = h.group(2).strip()
            if not p["title"]: p["errors"].append("empty title")
        u = re.search(r"^- \*\*URL:\*\* (\S+)$", body, re.M)
        if not u: p["errors"].append("missing URL")
        else:
            p["url"] = u.group(1)
            pu = urlparse(p["url"])
            if pu.scheme not in ("http", "https") or not pu.netloc: p["errors"].append("URL is not absolute")
        c = re.search(r"^### Content\n(.*?)^### Images \((\d+)\)\n(.*)$", body, re.S | re.M)
        if not c:
            p["errors"].append("missing '### Content' / '### Images (N)' sections")
        else:
            content = c.group(1).strip()
            words = len(re.findall(r"\w+", content))
            p["content_words"] = words
            p["no_source_text"] = "the original page has no body text" in content
            if p["no_source_text"]:
                p.setdefault("warnings", []).append("original page has no body text (video/images only) – documented in the file")
            elif not content or words < 5:
                p["errors"].append(f"content empty or too short ({words} words)")
            imgs = re.findall(r"^\d+\. (\S+)$", c.group(3), re.M)
            p["images"] = imgs
            if int(c.group(2)) != len(imgs): p["errors"].append(f"image header says {c.group(2)} but {len(imgs)} listed")
            bad = [x for x in imgs if not re.match(r"^https?://[^/\s]+/", x)]
            if bad: p["errors"].append(f"{len(bad)} image links are not full URLs")
        pages.append(p)
    nums = [p["n"] for p in pages]
    if nums != list(range(1, len(pages) + 1)):
        errors.append("page numbers are not consecutive from 1")
    if fm.get("page_count") and int(fm["page_count"]) != len(pages):
        errors.append(f"front matter page_count={fm['page_count']} but {len(pages)} pages found")
    if fm.get("image_count") and int(fm["image_count"]) != sum(len(p.get("images", [])) for p in pages):
        errors.append("front matter image_count does not match the image lists")
    return fm, pages, errors

def parse_sitemap(path):
    root = ET.parse(path).getroot()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [e.text.strip() for e in root.findall("s:url/s:loc", ns)]

def compare(sitemap, md_urls):
    s_set, m_set = set(sitemap), set(md_urls)
    missing = [u for u in sitemap if u not in m_set]
    extra = [u for u in md_urls if u not in s_set]
    dup_md = sorted({u for u in md_urls if md_urls.count(u) > 1})
    common_s = [u for u in sitemap if u in m_set]
    common_m = [u for u in md_urls if u in s_set]
    first_diff = next((i for i, (a, b) in enumerate(zip(common_s, common_m)) if a != b), None)
    return {"sitemap_urls": len(sitemap), "md_urls": len(md_urls), "missing_in_md": missing,
            "extra_in_md": extra, "duplicated_in_md": dup_md,
            "same_order": first_diff is None and not missing and not extra,
            "first_order_mismatch": None if first_diff is None else {"position": first_diff + 1, "sitemap": common_s[first_diff], "md": common_m[first_diff]}}

_session = None
def session():
    global _session
    if _session is None:
        _session = requests.Session(); _session.headers.update(UA)
        a = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=1)
        _session.mount("https://", a); _session.mount("http://", a)
    return _session

import threading
_host_locks = {}
_hl = threading.Lock()
PER_HOST = 4                       # be gentle: shared hosting answers 508 when flooded
RETRY_STATUS = {429, 502, 503, 504, 508}

def host_sem(url):
    h = urlparse(url).netloc
    with _hl:
        return _host_locks.setdefault(h, threading.Semaphore(PER_HOST))

def check_url(url, kind):
    """Check one URL; retries with backoff on rate-limit / overload answers."""
    res = None
    for attempt in range(5):
        with host_sem(url):
            res = _check_url(url, kind)
        if res.get("status") not in RETRY_STATUS and res.get("status") is not None:
            return res
        time.sleep(2 * (attempt + 1))
    res["note"] = (res.get("note", "") + " (after 5 attempts)").strip()
    return res

def _check_url(url, kind):
    try:
        if kind == "image":
            r = session().head(url, timeout=30, allow_redirects=True)
            if r.status_code in (403, 405) or r.status_code >= 500:
                r = session().get(url, timeout=30, allow_redirects=True, stream=True); r.close()
            ctype = r.headers.get("content-type", "")
            ok = r.status_code == 200 and (ctype.startswith("image/") or ctype == "application/octet-stream")
            return {"url": url, "status": r.status_code, "content_type": ctype, "ok": ok}
        r = session().get(url, timeout=45, allow_redirects=True)
        same_host = urlparse(r.url).netloc.replace("www.", "") == urlparse(url).netloc.replace("www.", "")
        ok = r.status_code == 200 and same_host
        return {"url": url, "status": r.status_code, "final_url": r.url, "ok": ok,
                "note": "" if same_host else "redirects to another host"}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)[:150]}

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    md_dir = args[0] if args else "md"
    do_http = "--no-http" not in sys.argv
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 32
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md") and not f.startswith("validation_report"))
    report, all_ok = {"generated": time.strftime("%Y-%m-%d %H:%M"), "sites": {}}, True
    for f in files:
        key = f[:-3]
        fm, pages, errs = parse_md(os.path.join(md_dir, f))
        sm_path = os.path.join(md_dir, fm.get("sitemap", key + ".sitemap.xml"))
        site = {"file": f, "pages": len(pages), "structure_errors": errs,
                "page_errors": {p["n"]: p["errors"] for p in pages if p["errors"]},
                "page_warnings": {p["n"]: p.get("warnings") for p in pages if p.get("warnings")}}
        if os.path.exists(sm_path):
            site["sitemap"] = compare(parse_sitemap(sm_path), [p.get("url") for p in pages])
        else:
            site["sitemap"] = {"error": f"sitemap file not found: {sm_path}"}
        imgs = sorted({u for p in pages for u in p.get("images", [])})
        site["unique_images"] = len(imgs)
        if do_http:
            with ThreadPoolExecutor(workers) as ex:
                pr = list(ex.map(lambda u: check_url(u, "page"), [p["url"] for p in pages if p.get("url")]))
                ir = list(ex.map(lambda u: check_url(u, "image"), imgs))
            site["http"] = {"pages_ok": sum(r["ok"] for r in pr), "pages_failed": [r for r in pr if not r["ok"]],
                            "images_ok": sum(r["ok"] for r in ir), "images_failed": [r for r in ir if not r["ok"]]}
        sm = site["sitemap"]
        site["structure_ok"] = not errs and not site["page_errors"]
        site["sitemap_ok"] = bool(sm.get("same_order")) or (sm.get("sitemap_urls") == 0 and sm.get("md_urls") == 0)
        all_ok &= site["structure_ok"] and site["sitemap_ok"]
        report["sites"][key] = site
        h = site.get("http", {})
        print(f"{key:26} pages={len(pages):4} structure={'OK' if site['structure_ok'] else 'FAIL'} "
              f"sitemap={'OK' if site['sitemap_ok'] else 'FAIL'}"
              + (f" pages_http={h['pages_ok']}/{len(pages)} images_http={h['images_ok']}/{len(imgs)}" if h else ""), flush=True)
    json.dump(report, open(os.path.join(md_dir, "validation_report.json"), "w"), ensure_ascii=False, indent=1)
    write_md_report(report, os.path.join(md_dir, "validation_report.md"))
    print("\nALL STRUCTURE + SITEMAP CHECKS PASSED" if all_ok else "\nSOME CHECKS FAILED – see validation_report.md")
    sys.exit(0 if all_ok else 1)

def write_md_report(rep, path):
    L = [f"# Validation report", "", f"Generated: {rep['generated']}", "",
         "| Site | Pages | Structure | Sitemap match & order | Pages load (HTTP 200) | Images load |", "|---|---|---|---|---|---|"]
    for k, s in rep["sites"].items():
        h = s.get("http")
        L.append(f"| {k} | {s['pages']} | {'✅' if s['structure_ok'] else '❌'} | {'✅' if s['sitemap_ok'] else '❌'} | "
                 + (f"{h['pages_ok']}/{s['pages']} | {h['images_ok']}/{s['unique_images']} |" if h else "– | – |"))
    for k, s in rep["sites"].items():
        issues = []
        issues += [f"- structure: {e}" for e in s["structure_errors"]]
        issues += [f"- page {n}: {', '.join(e)}" for n, e in s["page_errors"].items()]
        issues += [f"- page {n} (warning): {', '.join(w)}" for n, w in s.get("page_warnings", {}).items()]
        sm = s["sitemap"]
        for u in sm.get("missing_in_md", []): issues.append(f"- in sitemap but missing from Markdown: {u}")
        for u in sm.get("extra_in_md", []): issues.append(f"- in Markdown but not in sitemap: {u}")
        if sm.get("first_order_mismatch"): issues.append(f"- order differs from sitemap at position {sm['first_order_mismatch']['position']}")
        h = s.get("http", {})
        for r in h.get("pages_failed", []): issues.append(f"- page does not load: {r['url']} → HTTP {r.get('status')} {r.get('note') or r.get('error','')}".rstrip())
        fails = h.get("images_failed", [])
        for r in fails[:50]: issues.append(f"- image does not load: {r['url']} → HTTP {r.get('status')} {r.get('content_type') or r.get('error','')}".rstrip())
        if len(fails) > 50: issues.append(f"- … and {len(fails)-50} more failed images (see validation_report.json)")
        if issues:
            L += ["", f"## {k}", ""] + issues
    open(path, "w").write("\n".join(L) + "\n")

if __name__ == "__main__":
    main()
