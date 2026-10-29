#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the static review site from the five source documents.
Standard library only (plus the system openssl binary for AES). Run: python3 tools/build.py
Reads the five source files from source/, writes HTML into site/.
"""
import os, re, sys, json, zipfile, html, collections, subprocess, unicodedata, secrets, hashlib, base64, stat
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SITE = os.path.join(ROOT, 'site')
SRC = os.path.join(ROOT, 'source')
BUILD = os.path.dirname(os.path.abspath(__file__))   # tools/ (reports, helper scripts)
CONFIG = json.load(open(os.path.join(BUILD, 'config.json'), encoding='utf-8')) if os.path.exists(os.path.join(BUILD, 'config.json')) else {}
PASSWORD_FILE = os.path.join(ROOT, '.site-password')
SALT_FILE = os.path.join(ROOT, '.site-salt')

DOCS = [
    dict(id='01-brief', file='בריף UI.UX.pdf', kind='pdf', title='בריף UI/UX',
         nav='בריף', desc='הפנייה הפרטנית של משרד התיירות: רקע, השירות הנדרש, לוחות זמנים ואמות מידה לבחירת היועץ.'),
    dict(id='02-strategy', file='אסטרטגיית תוכן מקיפה - פלטפורמת התיירות הלאומית.docx', kind='docx', title='אסטרטגיית תוכן מקיפה',
         nav='אסטרטגיה', desc='המסמך המאוחד: החלטות סופיות, מפת האתר, תבניות, תרגום, משילות, הבסיס המחקרי ונספחים לחברת הפיתוח.'),
    dict(id='03-market-survey', file='סקר שוק  מערכת שיווק תיירותית לאומית, רב־שוקית ורב־לשונית.md', kind='md', title='סקר שוק',
         nav='סקר שוק', desc='בנצ\'מרק של 22 אתרי תיירות לאומיים: ציונים, ניתוח רוחבי, ספריית דפוסים והשלכות לפרויקט.'),
    dict(id='04-meeting', file='תמלול פגישה - אתרי שיווק - יועץ UIUX - 19.10.2026.docx', kind='docx', title='תמלול פגישה 19.10.2026',
         nav='תמלול פגישה', desc='תמלול הפגישה עם המשרד על הפערים בין הבריף לתוצרים, הדרישות הטכניות ולוחות הזמנים.'),
    dict(id='05-client-response', file='מענה להערות הלקוחה.docx', kind='docx', title='מענה להערות הלקוחה',
         nav='מענה להערות', desc='ריכוז כל הערות הלקוחה על שלושת המסמכים, הטיפול בכל אחת, סטטוסים ושאלות פתוחות.'),
]
DOC_BY_ID = {d['id']: d for d in DOCS}

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

def esc(s):
    return html.escape(s, quote=False)

def slugify(text, used):
    t = unicodedata.normalize('NFKC', text).strip().lower()
    t = re.sub(r'[֑-ׇ]', '', t)              # strip niqqud
    t = re.sub(r'[^\w]+', '-', t, flags=re.UNICODE).strip('-')
    t = re.sub(r'-{2,}', '-', t)[:80].strip('-') or 'section'
    if t[0].isdigit() or t[0] == '-':
        t = 's-' + t
    base = t; n = 1
    while t in used:
        n += 1; t = f'{base}-{n}'
    used.add(t)
    return t

# --------------------------------------------------------------------------
# Block model: list of dicts {type, html(inline), level, cells:[...], items...}
# types: h (level 1..4), p, li (level, ordered), tr (cells, header)
# Each unit (h, p, li, tr) becomes one numbered block in the output.
# --------------------------------------------------------------------------

class Doc:
    def __init__(self, meta):
        self.meta = meta
        self.id = meta['id']
        self.blocks = []          # sequence of blocks
        self.anchors = {}         # name -> block index (for bookmarks)
        self.used_ids = set()
        self.refs = []            # cross refs found
        self.unsure = []
        self.internal_links = 0
        self.stats = collections.Counter()

    def add(self, b):
        self.blocks.append(b)
        return len(self.blocks) - 1

# ---------------------------------- DOCX ----------------------------------

def run_on(rpr, tag):
    el = rpr.find(W + tag) if rpr is not None else None
    if el is None:
        return False
    v = el.get(W + 'val')
    return v not in ('0', 'false', 'none')

def docx_runs(parent, rels, doc, strip_url_prefixes):
    """Return inline HTML for the runs inside a paragraph (or hyperlink)."""
    out = []
    for child in parent:
        tag = child.tag
        if tag == W + 'r':
            rpr = child.find(W + 'rPr')
            txt = []
            for t in child:
                if t.tag == W + 't':
                    txt.append(esc(t.text or ''))
                elif t.tag == W + 'tab':
                    txt.append(' ')
                elif t.tag == W + 'br':
                    txt.append('<br>')
                elif t.tag == W + 'noBreakHyphen':
                    txt.append('‑')
            s = ''.join(txt)
            if not s:
                continue
            if run_on(rpr, 'b') and s.strip():
                s = f'<strong>{s}</strong>'
            if run_on(rpr, 'i') and s.strip():
                s = f'<em>{s}</em>'
            out.append(s)
        elif tag == W + 'hyperlink':
            inner = docx_runs(child, rels, doc, strip_url_prefixes)
            anchor = child.get(W + 'anchor')
            rid = child.get(R + 'id')
            if anchor:
                out.append(f'<a href="#BM:{esc(anchor)}">{inner}</a>')
            elif rid and rid in rels:
                url = rels[rid]
                if any(url.startswith(p) and '#' not in url for p in strip_url_prefixes):
                    out.append(inner)          # broken export link to the Google Doc root: keep text only
                else:
                    out.append(f'<a href="{esc(url)}">{inner}</a>')
            else:
                out.append(inner)
        elif tag == W + 'bookmarkStart':
            name = child.get(W + 'name')
            if name:
                out.append(f'<!--BM:{esc(name)}-->')
        elif tag in (W + 'smartTag', W + 'ins', W + 'sdt', W + 'sdtContent'):
            out.append(docx_runs(child, rels, doc, strip_url_prefixes))
    return ''.join(out)

def clean_inline(s):
    s = re.sub(r'(</strong>)(\s*)(<strong>)', r'\2', s)
    s = re.sub(r'(</em>)(\s*)(<em>)', r'\2', s)
    s = re.sub(r'(<br>\s*)+$', '', s)
    s = re.sub(r'^(\s*<br>)+', '', s)
    return s.strip()

def para_props(p):
    ppr = p.find(W + 'pPr')
    style = None; ilvl = 0; numid = None
    if ppr is not None:
        ps = ppr.find(W + 'pStyle')
        if ps is not None:
            style = ps.get(W + 'val')
        numpr = ppr.find(W + 'numPr')
        if numpr is not None:
            il = numpr.find(W + 'ilvl'); ni = numpr.find(W + 'numId')
            ilvl = int(il.get(W + 'val')) if il is not None else 0
            numid = ni.get(W + 'val') if ni is not None else None
    return style, ilvl, numid

def convert_docx(doc):
    path = os.path.join(SRC, doc.meta['file'])
    z = zipfile.ZipFile(path)
    rels = {}
    relx = ET.fromstring(z.read('word/_rels/document.xml.rels'))
    for rel in relx:
        if rel.get('TargetMode') == 'External':
            rels[rel.get('Id')] = rel.get('Target')
    numfmt = {}
    if 'word/numbering.xml' in z.namelist():
        nx = ET.fromstring(z.read('word/numbering.xml'))
        absfmt = {}
        for an in nx.findall(W + 'abstractNum'):
            lv = an.find(W + 'lvl')
            f = lv.find(W + 'numFmt') if lv is not None else None
            absfmt[an.get(W + 'abstractNumId')] = f.get(W + 'val') if f is not None else 'bullet'
        for n in nx.findall(W + 'num'):
            a = n.find(W + 'abstractNumId')
            numfmt[n.get(W + 'numId')] = absfmt.get(a.get(W + 'val'), 'bullet') if a is not None else 'bullet'
    root = ET.fromstring(z.read('word/document.xml'))
    body = root.find(W + 'body')
    strip_prefixes = ['https://docs.google.com/document/d/']
    seen_title = False
    pending_bm = []

    def handle_bookmarks(inline, idx):
        nonlocal pending_bm
        for name in re.findall(r'<!--BM:([^>]*)-->', inline):
            doc.anchors.setdefault(name, idx)
        for name in pending_bm:
            doc.anchors.setdefault(name, idx)
        pending_bm = []
        return re.sub(r'<!--BM:[^>]*-->', '', inline)

    for el in body:
        if el.tag == W + 'bookmarkStart':
            nm = el.get(W + 'name')
            if nm: pending_bm.append(nm)
            continue
        if el.tag == W + 'p':
            style, ilvl, numid = para_props(el)
            inline = clean_inline(docx_runs(el, rels, doc, strip_prefixes))
            plain = re.sub(r'<[^>]+>', '', inline).strip()
            if not plain:
                pending_bm += re.findall(r'<!--BM:([^>]*)-->', inline)
                continue
            idx = len(doc.blocks)
            inline = handle_bookmarks(inline, idx)
            if style and style.startswith('Heading') and style[7:].isdigit():
                lvl = int(style[7:]) + 1
                doc.add(dict(type='h', level=min(lvl, 4), html=inline))
            elif style == 'Title' or (not seen_title and style not in ('Heading1',)):
                doc.add(dict(type='h', level=1, html=inline)); seen_title = True
            elif numid is not None:
                doc.add(dict(type='li', level=ilvl, ordered=(numfmt.get(numid, 'bullet') != 'bullet'), html=inline))
            else:
                cls = 'subtitle' if (idx <= 3 and doc.blocks and doc.blocks[0]['type'] == 'h' and not any(b['type'] == 'h' and b['level'] > 1 for b in doc.blocks)) else None
                doc.add(dict(type='p', html=inline, cls=cls))
            seen_title = True
        elif el.tag == W + 'tbl':
            rows = el.findall(W + 'tr')
            first = True
            for tr in rows:
                cells = []
                for tc in tr.findall(W + 'tc'):
                    parts = []
                    for p in tc.findall(W + 'p'):
                        s = clean_inline(docx_runs(p, rels, doc, strip_prefixes))
                        s = re.sub(r'<!--BM:[^>]*-->', '', s)
                        if re.sub(r'<[^>]+>', '', s).strip():
                            parts.append(s)
                    cells.append('<br>'.join(parts))
                if not any(re.sub(r'<[^>]+>', '', c).strip() for c in cells):
                    continue
                idx = doc.add(dict(type='tr', cells=cells, header=first, table_start=first))
                for name in pending_bm:
                    doc.anchors.setdefault(name, idx)
                pending_bm = []
                first = False
            if not first:
                doc.blocks[-1]['table_end'] = True

# ----------------------------------- MD -----------------------------------

def md_inline(s):
    s = esc(s)
    codes = []
    def keep(m):
        codes.append(f'<code>{m.group(1)}</code>'); return f'\x00{len(codes)-1}\x00'
    s = re.sub(r'`([^`]+)`', keep, s)
    s = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'\x00(\d+)\x00', lambda m: codes[int(m.group(1))], s)
    return s

def convert_md(doc):
    path = os.path.join(SRC, doc.meta['file'])
    lines = open(path, encoding='utf-8').read().split('\n')
    i = 0; para = []
    def flush():
        nonlocal para
        if para:
            doc.add(dict(type='p', html=md_inline(' '.join(x.strip() for x in para))))
            para = []
    while i < len(lines):
        l = lines[i]
        if not l.strip():
            flush(); i += 1; continue
        m = re.match(r'^(#{1,6})\s+(.*)$', l)
        if m:
            flush(); doc.add(dict(type='h', level=len(m.group(1)), html=md_inline(m.group(2).strip()))); i += 1; continue
        if l.startswith('|'):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                row = lines[i].strip()
                if re.match(r'^\|\s*:?-{2,}', row):
                    i += 1; continue
                cells = [c.strip() for c in row.strip('|').split('|')]
                rows.append(cells); i += 1
            for k, cells in enumerate(rows):
                b = dict(type='tr', cells=[md_inline(c) for c in cells], header=(k == 0), table_start=(k == 0))
                doc.add(b)
            doc.blocks[-1]['table_end'] = True
            continue
        m = re.match(r'^(\s*)[-*]\s+(.*)$', l)
        if m:
            flush()
            doc.add(dict(type='li', level=len(m.group(1)) // 2, ordered=False, html=md_inline(m.group(2))))
            i += 1; continue
        if l.endswith('  ') and para is not None:
            para.append(l.rstrip() + '<br>')   # hard break
            i += 1; continue
        para.append(l); i += 1
    flush()
    # join hard breaks correctly (we appended '<br>' inside text; md_inline escapes it, fix)
    for b in doc.blocks:
        if b['type'] == 'p':
            b['html'] = b['html'].replace('&lt;br&gt;', '<br>')

# ----------------------------------- PDF ----------------------------------

PDF_JS = r'''
ObjC.import('Quartz');
function run(argv){
  var url=$.NSURL.fileURLWithPath(argv[0]); var doc=$.PDFDocument.alloc.initWithURL(url);
  var n=doc.pageCount; var out=[];
  for(var i=0;i<n;i++){
    var page=doc.pageAtIndex(i);
    var s=ObjC.unwrap(page.string)||'';
    var sel=page.selectionForRange($.NSMakeRange(0, s.length));
    var lines=sel?ObjC.unwrap(sel.selectionsByLine):[]; var res=[];
    for(var k=0;k<lines.length;k++){ var L=lines[k]; var b=L.boundsForPage(page);
      res.push({t:ObjC.unwrap(L.string), x:b.origin.x, y:b.origin.y, w:b.size.width, h:b.size.height}); }
    out.push(res);
  }
  return JSON.stringify(out);
}
'''
HEB = re.compile(r'[֐-׿]')

def logical_order(fragment):
    """PDFKit returns a fragment in left-to-right visual run order with Hebrew runs
    already readable. For an RTL line the logical order is the reverse run order."""
    toks = fragment.split(' ')
    runs = []
    for tok in toks:
        kind = 'h' if HEB.search(tok) else ('l' if re.search(r'[A-Za-z0-9]', tok) else 'n')
        if runs and (runs[-1][0] == kind or kind == 'n'):
            runs[-1][1].append(tok)
        elif runs and runs[-1][0] == 'n':
            runs[-1][0] = kind; runs[-1][1].append(tok)
        else:
            runs.append([kind, [tok]])
    runs.reverse()
    return ' '.join(' '.join(r[1]) for r in runs)

def convert_pdf(doc):
    path = os.path.join(SRC, doc.meta['file'])
    js = os.path.join(BUILD, 'pdflines.js')
    open(js, 'w').write(PDF_JS)
    raw = subprocess.run(['osascript', '-l', 'JavaScript', js, path], capture_output=True, text=True, check=True).stdout
    pages = json.loads(raw)
    doc.add(dict(type='h', level=1, html='בריף UI/UX: הגדרת תוכן ושפה עיצובית עבור מערכת להקמת אתרי שיווק'))
    doc.add(dict(type='p', cls='notice', html='הערת המרה: קובץ המקור הוא PDF שבו שכבת הטקסט שמורה בסדר חזותי משובש (קטעי הטקסט בכל שורה הפוכים). '
        'הטקסט שלהלן שוחזר אוטומטית לפי מיקום הקטעים בעמוד, ולכן ייתכנו שיבושים בסדר המילים, בעיקר בטבלאות. '
        'יש לאמת מול קובץ המקור. לשחזור נאמן נדרש קובץ ה-Word המקורי של הבריף.'))
    for pi, frags in enumerate(pages):
        doc.add(dict(type='h', level=2, html=f'עמוד {pi+1}'))
        frags = [f for f in frags if f['t'].strip()]
        frags.sort(key=lambda f: -f['y'])
        lines = []
        for f in frags:
            if lines and abs(lines[-1][0] - f['y']) < 5:
                lines[-1][1].append(f)
            else:
                lines.append([f['y'], [f]])
        for y, fs in lines:
            fs.sort(key=lambda f: -(f['x'] + f['w']))
            text = ' '.join(logical_order(f['t'].strip()) for f in fs)
            text = re.sub(r'\s+', ' ', text).strip()
            text = re.sub(r'^(\d) \. ', r'\1. ', text)
            text = re.sub(r' ([,.:;)])', r'\1', text)
            text = re.sub(r'\( ', '(', text)
            if not text:
                continue
            if re.match(r'^\d\. \S', text) and len(text) < 60:
                doc.add(dict(type='h', level=3, html=esc(text)))
            else:
                doc.add(dict(type='p', html=esc(text)))

# ------------------------------ ids & anchors -----------------------------

def plain(b):
    if b['type'] == 'tr':
        return ' | '.join(re.sub(r'<[^>]+>', '', c) for c in b['cells'])
    return re.sub(r'<[^>]+>', '', b.get('html', ''))

def assign_ids(doc):
    doc.used_ids = set()
    doc.heading_ids = []   # (level, text, id)
    for i, b in enumerate(doc.blocks):
        b['block'] = f"{doc.id}:{i}"
        b['id'] = None
        if b['type'] == 'h':
            text = html.unescape(plain(b))
            b['id'] = slugify(text, doc.used_ids)
            b['text'] = text
            doc.heading_ids.append((b['level'], text, b['id']))
    # special anchors
    doc.special = {}
    for i, b in enumerate(doc.blocks):
        txt = html.unescape(plain(b)).strip()
        aid = None
        if doc.id == '02-strategy':
            m = re.match(r'^ת-(\d+)\.', txt)
            if m and b['type'] == 'p': aid = f't-{m.group(1)}'
            if b['type'] == 'tr':
                c0 = html.unescape(re.sub(r'<[^>]+>', '', b['cells'][0])).strip()
                m = re.match(r'^ת-(\d+)$', c0)
                if m: aid = f't-{m.group(1)}-row'
                m = re.match(r'^ה-(\d+)$', c0)
                if m: aid = f'd-{m.group(1)}'
            m = re.match(r'^ש-(\d+)\.', txt)
            if m and b['type'] == 'li': aid = f'q-{m.group(1)}'
        elif doc.id == '05-client-response':
            if b['type'] == 'tr':
                c0 = html.unescape(re.sub(r'<[^>]+>', '', b['cells'][0])).strip()
                m = re.match(r"^(SEO|אס'|מח')-C(\d+)$", c0)
                if m:
                    pref = {'SEO': 'seo', "אס'": 'as', "מח'": 'mh'}[m.group(1)]
                    aid = f'c-{pref}-c{m.group(2)}'
            m = re.match(r'^ש-(\d+)\.', txt)
            if m and b['type'] == 'li': aid = f'q-{m.group(1)}'
        elif doc.id == '04-meeting':
            m = re.match(r'^\[(\d\d):(\d\d):(\d\d)\]', txt)
            if m: aid = f'ts-{m.group(1)}-{m.group(2)}-{m.group(3)}'
        elif doc.id == '01-brief':
            m = re.match(r'^(\d)\. ', txt)
            if m and b['type'] == 'h': aid = f'sec-{m.group(1)}'
        if aid and aid not in doc.used_ids and not b['id']:
            b['id'] = aid; doc.used_ids.add(aid); doc.special[aid] = i
        elif aid and aid not in doc.used_ids and b['id']:
            doc.special[aid] = i   # heading already has slug id; keep alias map
            b.setdefault('alias', aid)
            doc.used_ids.add(aid)
    # resolve bookmark anchors -> ids
    for name, idx in doc.anchors.items():
        b = doc.blocks[idx] if idx < len(doc.blocks) else None
        if b is None: continue
        if not b['id']:
            b['id'] = b['block']
        doc.anchors[name] = b['id']

def anchor_of(doc, idx):
    b = doc.blocks[idx]
    if not b['id']:
        b['id'] = b['block']
    return b['id']

def find_heading(doc, pred):
    for i, b in enumerate(doc.blocks):
        if b['type'] == 'h' and pred(b['text']):
            return anchor_of(doc, i)
    return None

def heading_by_prefix(doc, prefix):
    return find_heading(doc, lambda t: t.startswith(prefix))

# ----------------------------- cross references ---------------------------

def link_text_segments(htmls, fn):
    """Apply fn(text)->text to text segments that are outside <a> elements and outside tags."""
    out = []; depth = 0
    for part in re.split(r'(<[^>]+>)', htmls):
        if not part: continue
        if part.startswith('<'):
            if re.match(r'<a[\s>]', part): depth += 1
            elif part.startswith('</a'): depth -= 1
            out.append(part)
        else:
            out.append(part if depth > 0 else fn(part))
    return ''.join(out)

def make_linker(doc, docs):
    strat = docs['02-strategy']; brief = docs['01-brief']; survey = docs['03-market-survey']
    meeting = docs['04-meeting']; resp = docs['05-client-response']
    ts_list = sorted((k, v) for k, v in meeting.special.items() if k.startswith('ts-'))

    def meeting_ts(mm, ss):
        target = int(mm) * 60 + int(ss)
        best = None
        for k, _ in ts_list:
            _, h, m, s = k.split('-')
            t = int(h) * 3600 + int(m) * 60 + int(s)
            if t <= target: best = k
            else: break
        return best

    def strat_chapter(num):
        # "3.6" or "11" -> heading id
        if '.' in num:
            return heading_by_prefix(strat, num + ' ')
        return heading_by_prefix(strat, num + '. ')

    def strat_appendix(letter, sub=None):
        if sub:
            return heading_by_prefix(strat, f'{letter}.{sub} ')
        return heading_by_prefix(strat, f"נספח {letter}'")

    def resp_chapter(num):
        if '.' in num:
            return heading_by_prefix(resp, num + ' ')
        return heading_by_prefix(resp, num + '. ')

    records = []   # (text, target_doc, anchor, sure)

    def link(text, tdoc, anchor, sure=True, cls='xref'):
        if tdoc == doc.id:
            doc.internal_links += 1
            href = f'#{anchor}' if anchor else '#'
            return f'<a href="{href}" class="iref">{text}</a>'
        href = f'{tdoc}.html' + (f'#{anchor}' if anchor else '')
        records.append((html.unescape(text), tdoc, anchor, sure))
        return f'<a href="{href}" class="xref" data-xref="1">{text}</a>'

    def brief_section(n):
        return brief.special.get(f'sec-{n}') is not None and f'sec-{n}' or None

    def sub(pattern, repl, text, flags=0):
        return link_text_segments(text, lambda seg: re.sub(pattern, repl, seg, flags=flags))

    def fn(text):
        # --- references to the strategy document (from response, or internal) ---
        if doc.id in ('05-client-response', '02-strategy'):
            tdoc = '02-strategy'
            # chapter numbers: "פרק 3.6", "פרקים 12 ו-14", "פרק 1.3, 13.2, 13.3"
            def chap(m):
                head = m.group(1); nums = m.group(2)
                def one(mm):
                    a = strat_chapter(mm.group(1)) if not (doc.id == '05-client-response' and head.endswith('כאן')) else None
                    if a: return link(mm.group(1), tdoc, a)
                    return mm.group(1)
                return head + sub(r'(\d+(?:\.\d+)?)', one, nums)
            text = sub(r'(פרקים? |בפרקים? |ובפרק |לפרק )((?:\d+(?:\.\d+)?(?:[,–]\s*| ו-| עד | ו)?)+)', chap, text)
            # appendices: "נספח א'.6", "נספח א'", "נספח ב' ש-1", "(ג.4)"
            def app(m):
                letter = m.group(1); sub = m.group(2)
                a = strat_appendix(letter, sub)
                if a: return link(m.group(0), tdoc, a)
                return m.group(0)
            text = sub(r"נספח ([אבג])'(?:\.(\d+))?", app, text)
            if doc.id == '02-strategy':
                text = sub(r'\((ג)\.(\d+)\)', lambda m: '(' + (link(f'{m.group(1)}.{m.group(2)}', tdoc, strat_appendix(m.group(1), m.group(2))) if strat_appendix(m.group(1), m.group(2)) else m.group(0)[1:-1]) + ')', text)
            # insights ת-N (not the definition itself)
            def ins(m):
                n = m.group(1); a = f't-{n}' if f't-{n}' in strat.special else (f't-{n}-row' if f't-{n}-row' in strat.special else None)
                if a: return link(m.group(0), tdoc, a)
                return m.group(0)
            if not re.match(r'^ת-\d+\.', text.strip()):
                text = sub(r'(?<![\w-])ת-(\d+)(?!\d)', ins, text)
            # decisions ה-N
            def dec(m):
                n = m.group(2); a = f'd-{n}' if f'd-{n}' in strat.special else None
                if a: return m.group(1) + link(f'ה-{n}', tdoc, a)
                return m.group(0)
            text = sub(r'(החלטה |החלטות |ובהחלטה |בהחלטה |ה-1, ו)(?:ה-)?(\d+)(?!\d)', lambda m: dec(m) if 'ה-' in m.group(0) or m.group(1).startswith('החלט') else m.group(0), text)
            # questions ש-N
            def q(m):
                n = m.group(1); a = f'q-{n}' if f'q-{n}' in strat.special else None
                if a and doc.id == '02-strategy': return link(m.group(0), tdoc, a)
                if a and doc.id == '05-client-response' and 'נספח' in text: return link(m.group(0), tdoc, a)
                return m.group(0)
            text = sub(r'(?<![\w-])ש-(\d+)(?!\d)', q, text)
        # --- response doc: own chapters "בפרק 5 כאן" ---
        if doc.id == '05-client-response':
            text = sub(r'(ובפרק |בפרק )(\d+)( כאן)', lambda m: m.group(1) + (link(m.group(2), doc.id, resp_chapter(m.group(2))) if resp_chapter(m.group(2)) else m.group(2)) + m.group(3), text)
            text = sub(r'(המסמך המאוחד|"אסטרטגיית תוכן מקיפה"|המסמך המעודכן)', lambda m: link(m.group(1), '02-strategy', None), text)
        # --- comments SEO-Cn / אס'-Cn / מח'-Cn  -> response rows ---
        if doc.id in ('02-strategy',):
            def cm(m):
                pref = {'SEO': 'seo', "אס'": 'as', "מח'": 'mh'}[m.group(1)]
                a = f'c-{pref}-c{m.group(2)}'
                if a in resp.special: return link(m.group(0), '05-client-response', a)
                return m.group(0)
            text = sub(r"(SEO|אס'|מח')-C(\d+)", cm, text)
            def bare(m):
                n = m.group(1)
                hits = [k for k in resp.special if re.match(rf'c-(seo|as|mh)-c{n}$', k)]
                if len(hits) == 1: return link(m.group(0), '05-client-response', hits[0])
                if len(hits) > 1:
                    doc.unsure.append((m.group(0), '05-client-response', 'הערה מספר C' + n + ' קיימת בכמה מסמכי מקור'))
                    return link(m.group(0), '05-client-response', hits[0], sure=False)
                return m.group(0)
            text = sub(r"(?<![\w'-])C(\d+)(?!\d)", bare, text)
        # --- meeting transcript timestamps ---
        if doc.id in ('02-strategy', '05-client-response'):
            def ts(m):
                a = meeting_ts(m.group(2), m.group(3))
                if a: return m.group(1) + link(m.group(2) + ':' + m.group(3), '04-meeting', a)
                return m.group(0)
            text = sub(r'(תמלול |תמלול, |תמלול hh:mm, )?(\d{1,2}):(\d{2})(?!\d)', lambda m: ts(m) if m.group(1) else m.group(0), text)
            text = sub(r'(?<!>)(תמלול מ-19\.10|התמלול|הקובץ המתומלל|תמלול הפגישה|תמלול הפגישה מ-19\.10\.2026|לפי התמלול)(?![^<]*</a>)', lambda m: link(m.group(1), '04-meeting', None), text)
        # --- the brief ---
        if doc.id in ('02-strategy', '04-meeting', '05-client-response'):
            def br(m):
                word = m.group(0)
                # section hint: "סעיף 6 של הבריף"
                return link(word, '01-brief', None)
            text = sub(r'(סעיף (\d) של )(הבריף)', lambda m: m.group(1) + (link(m.group(3), '01-brief', f'sec-{m.group(2)}') if brief_section(m.group(2)) else link(m.group(3), '01-brief', None)), text)
            text = sub(r'(?<![\w])(?:ה|ב|מ|ל|וב|ומ|ש|כ)?בריף(?![\w])(?! UI)', br, text)
            text = sub(r'\[בריף\]', lambda m: '[' + link('בריף', '01-brief', None) + ']', text) if '[בריף]' in text else text
        # --- the market survey (scorecard, benchmark) ---
        if doc.id in ('02-strategy', '04-meeting', '05-client-response'):
            sc = heading_by_prefix(survey, 'Master scorecard')
            text = sub(r'(?<![\w])(Master Scorecard|\[Scorecard\]|Scorecard)(?![\w])', lambda m: ('[' + link('Scorecard', '03-market-survey', sc) + ']') if m.group(1) == '[Scorecard]' else link(m.group(1), '03-market-survey', sc), text)
            if doc.id == '04-meeting':
                text = sub(r'(סקר השוק|סקר שוק)', lambda m: link(m.group(1), '03-market-survey', None), text)
            if doc.id == '02-strategy':
                def sv(m):
                    doc.unsure.append((m.group(0), '03-market-survey', 'עשוי להתייחס לדרישת סקר השוק בבריף ולא למסמך הסקר עצמו'))
                    return link(m.group(0), '03-market-survey', None, sure=False)
                text = sub(r'(סקר השוק|סקר שוק של 5 אתרי תיירות לפחות)', sv, text)
        # --- survey internal section references ---
        if doc.id == '03-market-survey':
            m1 = heading_by_prefix(survey, 'מתודולוגיה'); m2 = heading_by_prefix(survey, 'Appendix')
            m3 = heading_by_prefix(survey, 'F. Media'); m4 = heading_by_prefix(survey, 'השלכות לפרויקט')
            if m1: text = sub(r'בפרק המתודולוגיה', lambda m: link(m.group(0), doc.id, m1), text)
            if m2: text = sub(r'(?<![\w])(ובנספח|בנספח)(?![\w])', lambda m: link(m.group(0), doc.id, m2), text)
            if m3: text = sub(r'בפרק F(?![\w])', lambda m: link(m.group(0), doc.id, m3), text)
            if m4: text = sub(r'בסעיף ההשלכות', lambda m: link(m.group(0), doc.id, m4), text)
        return text

    return fn, records

def apply_links(doc, docs):
    fn, records = make_linker(doc, docs)
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [link_text_segments(c, fn) for c in b['cells']]
        else:
            if doc.id == '01-brief' and b.get('cls') == 'notice':
                continue
            b['html'] = link_text_segments(b['html'], fn)
    # resolve docx bookmark links
    def norm(t):
        return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', t))).strip().lower()
    head_by_text = {}
    for b in doc.blocks:
        if b['type'] == 'h':
            head_by_text.setdefault(norm(b['html']), b['id'])
    doc.dead_links = 0
    def bm(m):
        name = m.group(1); text = m.group(2)
        target = doc.anchors.get(name)
        if not target:
            target = head_by_text.get(norm(text))
        if not target:
            # TOC lines like "1.1 תקציר" -> heading starting with the same number
            mm = re.match(r'^([\d]+(?:\.[\d]+)?|[אבג]\.\d+|נספח [אבג]\')\.?\s', norm(text))
            if mm:
                for b in doc.blocks:
                    if b['type'] == 'h' and norm(b['html']).startswith(mm.group(1)):
                        target = b['id']; break
        if target:
            doc.internal_links += 1
            return f'<a href="#{target}" class="iref">{text}</a>'
        doc.dead_links += 1
        return text
    pat = r'<a href="#BM:([^"]+)">(.*?)</a>'
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [re.sub(pat, bm, c, flags=re.S) for c in b['cells']]
        else:
            b['html'] = re.sub(pat, bm, b['html'], flags=re.S)
    doc.refs = records
    # drop links whose target is not http(s), mailto, an in-page anchor or one of our pages
    safe_href = re.compile(r'^(https?://|mailto:|#|0\d-[a-z-]+\.html(#|$)|index\.html$)')
    def sanitize(sv):
        return re.sub(r'<a href="([^"]*)"[^>]*>(.*?)</a>',
                      lambda m: m.group(0) if safe_href.match(html.unescape(m.group(1))) else m.group(2), sv, flags=re.S)
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [sanitize(c) for c in b['cells']]
        else:
            b['html'] = sanitize(b['html'])
    # external links open in a new tab
    ext_pat = re.compile(r'<a href="(https?://[^"]+)"(?![^>]*target=)')
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [ext_pat.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer"', c) for c in b['cells']]
        else:
            b['html'] = ext_pat.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer"', b['html'])
    # merge adjacent links with the same target (Word split "ת" and "-33" into two hyperlinks)
    merge_pat = re.compile(r'(<a href="([^"]+)" class="iref">)([^<]*)</a>(<a href="\2" class="iref">)')
    def merge(sv):
        prev = None
        while prev != sv:
            prev = sv
            sv = merge_pat.sub(lambda m: m.group(1) + m.group(3), sv)
        return sv
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [merge(c) for c in b['cells']]
        else:
            b['html'] = merge(b['html'])
    # table of contents: plain paragraphs before the first section heading that repeat a heading -> link
    if doc.id == '02-strategy':
        def norm2(t):
            return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', t))).strip().lower()
        heads = {}
        for b in doc.blocks:
            if b['type'] == 'h' and b['level'] > 1:
                heads.setdefault(norm2(b['html']), b['id'])
        in_toc = False
        for b in doc.blocks:
            if b['type'] == 'h' and b['level'] > 1:
                if in_toc: break
                in_toc = norm2(b['html']).startswith('תוכן עניינים')
                continue
            if in_toc and b['type'] == 'p':
                key = norm2(b['html'])
                if key in heads and '<a ' not in b['html']:
                    b['html'] = f'<a href="#{heads[key]}" class="iref">{b["html"]}</a>'
                    doc.internal_links += 1

# ------------------------------ decoration --------------------------------
# Presentation-only markup. Each step wraps existing text in spans and never changes
# the text itself, so the text hash that comments keep (blockHash in app.js) still matches.

TAG_RE = re.compile('|'.join(f'(?P<{name}>{pat})' for name, pat in [
    ('obs', r'\[OBSERVED(?:, [^\]<]{1,30})?\]'),
    ('inf', r'\[INFERRED(?:, [^\]<]{1,30})?\]'),
    ('rep', r'\[REPORTED(?:, [^\]<]{1,30})?\]'),
    ('scope', r'\[הרחבת מכרז\]'),
    ('ask', r'\[דורש בירור\]'),
    ('unv', r'\[(?:לא מאומת|לא ברור)\]'),
]))
STATUS_RE = re.compile(r'^((?:<[^>]+>)*\s*)(בוצע|דורש בירור|סותר החלטה סופית)')
STATUS_CLS = {'בוצע': 'done', 'דורש בירור': 'ask', 'סותר החלטה סופית': 'conflict'}
SCORES = {'1', '2', '3', '4', '5', 'n.a.'}
TS_RE = re.compile(r'^(\[\d\d:\d\d:\d\d\])(.*)$', re.S)
HNUM_RE = re.compile(r'^((?:<[^>]+>)*)(\d+(?:\.\d+)*\.?|[א-ת]\.\d+|נספח [א-ת]\')(?=[\s:])')
RID_RE = re.compile(r"^[A-Za-z\u0590-\u05ff']{1,5}-[A-Za-z]?\d{1,3}$")
TOC_NUM_RE = re.compile(r'^(<a [^>]*>)(\d+(?:\.\d+)?\.?|[א-ת]\.\d+|(?:חלק|נספח) [א-ת]\':?)(\s+)(.*)(</a>)$', re.S)

def on_text(s, fn):
    """Apply fn to the text between tags only."""
    return ''.join(part if part.startswith('<') else fn(part) for part in re.split(r'(<[^>]+>)', s))

def tagify(s):
    return on_text(s, lambda t: TAG_RE.sub(lambda m: f'<span class="tag tag-{m.lastgroup}">{m.group(0)}</span>', t))

def cell_text(c):
    return html.unescape(re.sub(r'<[^>]+>', '', c)).strip()

def tables(doc):
    groups = []
    for b in doc.blocks:
        if b['type'] != 'tr':
            continue
        if b.get('table_start') or not groups:
            groups.append([])
        groups[-1].append(b)
    return groups

def decorate(doc):
    for b in doc.blocks:
        if b['type'] == 'tr':
            b['cells'] = [tagify(c) for c in b['cells']]
        else:
            b['html'] = tagify(b['html'])
    for rows in tables(doc):
        head = [cell_text(c) for c in rows[0]['cells']] if rows[0].get('header') else []
        body = [r for r in rows if not r.get('header')]
        # status column (client response): colour the leading status word
        if 'סטטוס' in head:
            i = head.index('סטטוס')
            for r in body:
                if i < len(r['cells']):
                    r['cells'][i] = STATUS_RE.sub(lambda m: f'{m.group(1)}<span class="stat stat-{STATUS_CLS[m.group(2)]}">{m.group(2)}</span>', r['cells'][i], count=1)
        # score columns (market survey): every body value is 1-5 or n.a.
        width = max((len(r['cells']) for r in body), default=0)
        for i in range(width):
            vals = [cell_text(r['cells'][i]) for r in body if i < len(r['cells'])]
            if len(vals) >= 2 and all(v in SCORES for v in vals) and any(v != 'n.a.' for v in vals):
                for r in body:
                    if i < len(r['cells']):
                        v = cell_text(r['cells'][i])
                        r['cells'][i] = f'<span class="score score-{"na" if v == "n.a." else v}">{r["cells"][i]}</span>'
    if doc.id == '01-brief':
        for b in doc.blocks:
            if b['type'] == 'h' and re.fullmatch(r'עמוד \d+', plain(b).strip()):
                b['cls'] = 'pagemark'
            elif b['type'] == 'p' and not b.get('cls') and re.fullmatch(r"עמ' \d+", plain(b).strip()):
                b['cls'] = 'pagefoot'
    if doc.id == '04-meeting':
        for b in doc.blocks:
            if b['type'] == 'p' and TS_RE.match(b['html']):
                b['cls'] = 'line'
                b['html'] = TS_RE.sub(r'<span class="ts">\1</span><span class="say">\2</span>', b['html'], count=1)
    if doc.id == '02-strategy':
        in_toc = False
        for b in doc.blocks:
            if b['type'] == 'h' and b['level'] == 2:
                if in_toc: break
                in_toc = plain(b).strip().startswith('תוכן עניינים')
                continue
            if not in_toc or b['type'] != 'p':
                continue
            text = plain(b).strip()
            kind = 'part' if text.startswith('חלק ') else 'ch' if re.match(r'(\d+\.|נספח [א-ת]\')\s', text) else 'sub'
            b['toc'] = kind; b['cls'] = 'toc-' + kind
            h = re.sub(r'</?strong>', '', b['html'])
            b['html'] = TOC_NUM_RE.sub(r'\1<span class="toc-num">\2</span>\3<span class="toc-t">\4</span>\5', h, count=1)
        for b in doc.blocks:
            if b['type'] == 'h' and b['level'] == 2 and plain(b).strip().startswith('חלק '):
                b['cls'] = 'part'
    # numbered headings: the number gets its own span
    for b in doc.blocks:
        if b['type'] == 'h' and 2 <= b['level'] <= 4 and b.get('cls') != 'pagemark':
            b['html'] = HNUM_RE.sub(r'\1<span class="hnum">\2</span>', b['html'], count=1)
    # row identifiers (SEO-C4, ה-1) in the first column become chips
    for rows in tables(doc):
        body = [r for r in rows if not r.get('header') and r['cells']]
        if len(body) >= 2 and all(RID_RE.match(cell_text(r['cells'][0])) for r in body):
            for r in body:
                r['cells'][0] = f'<span class="rid">{r["cells"][0]}</span>'
    # the metadata line under the survey title reads as a subtitle
    if doc.id == '03-market-survey':
        for i, b in enumerate(doc.blocks[:-1]):
            if b['type'] == 'h' and b['level'] == 1:
                if doc.blocks[i + 1]['type'] == 'p' and not doc.blocks[i + 1].get('cls'):
                    doc.blocks[i + 1]['cls'] = 'subtitle'
                break
    # status totals for the client-response dashboard, counted from the response tables
    if doc.id == '05-client-response':
        kpis = collections.Counter()
        for rows in tables(doc):
            head = [cell_text(c) for c in rows[0]['cells']] if rows[0].get('header') else []
            if 'סטטוס' in head and "מס'" in head:
                i = head.index('סטטוס')
                for r in rows[1:]:
                    m = STATUS_RE.match(cell_text(r['cells'][i])) if i < len(r['cells']) else None
                    if m:
                        kpis[STATUS_CLS[m.group(2)]] += 1
        doc.kpis = kpis

def hero_parts(doc):
    """Opening and closing markup of the cover header that wraps the title and subtitles."""
    n = DOCS.index(doc.meta) + 1
    texts = [c for b in doc.blocks for c in (b['cells'] if b['type'] == 'tr' else [b.get('html', '')])]
    words = sum(len(cell_text(t).split()) for t in texts)
    chips = []
    if doc.id == '01-brief':
        chips.append((sum(1 for b in doc.blocks if b['type'] == 'h' and b['level'] == 3), 'סעיפים'))
        chips.append((sum(1 for b in doc.blocks if b.get('cls') == 'pagemark'), 'עמודים'))
    else:
        h2 = [b for b in doc.blocks if b['type'] == 'h' and b['level'] == 2 and b.get('cls') != 'part'
              and not plain(b).strip().startswith('תוכן עניינים')]
        if h2:
            chips.append((len(h2), 'פרקים'))
    if doc.id == '04-meeting':
        stamps = [TS_RE.match(cell_text(b['html'])).group(1) for b in doc.blocks if b['type'] == 'p' and TS_RE.match(cell_text(b['html']))]
        if stamps:
            h, m, sec = (int(x) for x in stamps[-1].strip('[]').split(':'))
            chips.append((round(h * 60 + m + sec / 60), 'דקות הקלטה'))
            chips.append((len(stamps), 'קטעי דיבור'))
    n_tables = len(tables(doc))
    if n_tables:
        chips.append((n_tables, 'טבלאות'))
    chips.append((max(1, round(words / 200)), 'דקות קריאה משוערות'))
    meta = ''.join(f'<li><strong>{esc(str(v))}</strong>{esc(label)}</li>' for v, label in chips)
    extra = ''
    k = getattr(doc, 'kpis', None)
    if k:
        total = sum(k.values())
        parts = [('done', 'בוצע'), ('ask', 'דורש בירור'), ('conflict', 'סותר החלטה סופית')]
        cards = f'<div class="kpi"><span class="v">{total}</span><span class="l">הערות שטופלו</span></div>'
        cards += ''.join(f'<div class="kpi kpi-{c}"><span class="v">{k[c]}</span><span class="l">{label}</span></div>' for c, label in parts)
        x = 100.0; rects = ''
        for c, _ in parts:
            w = 100.0 * k[c] / total
            x -= w
            rects += f'<rect class="seg-{c}" x="{x:.3f}" y="0" width="{w:.3f}" height="6"/>'
        extra = (f'<div class="kpis" role="group" aria-label="סיכום הטיפול בהערות">{cards}'
                 f'<svg class="kbar" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">{rects}</svg></div>')
    open_html = (f'<header class="dochero"><span class="dochero-no" aria-hidden="true">{n:02d}</span>'
                 f'<p class="dochero-eyebrow">מסמך {n:02d} מתוך {len(DOCS):02d}</p>')
    close_html = f'<ul class="docmeta" aria-label="נתוני המסמך">{meta}</ul>{extra}</header>'
    return open_html, close_html

# -------------------------------- rendering -------------------------------

def render_blocks(doc):
    out = []
    list_stack = []   # levels of open lists: (level, ordered)
    in_table = False
    toc = []          # open table-of-contents containers: (rank, closing tag)
    TOC_RANK = dict(nav=0, part=1, grid=2, ch=3, subs=4)

    def toc_close(rank):
        while toc and toc[-1][0] >= rank:
            out.append(toc.pop()[1])

    def toc_open(name, tag):
        out.append(tag); toc.append((TOC_RANK[name], '</nav>' if name == 'nav' else '</section>' if name == 'part' else '</div>'))

    def toc_enter(kind):
        if not toc:
            toc_open('nav', '<nav class="toc" aria-label="תוכן עניינים">')
        if kind == 'part':
            toc_close(1); toc_open('part', '<section class="toc-group">')
        elif kind == 'ch':
            toc_close(3)
            if toc[-1][0] < 2:
                toc_open('grid', '<div class="toc-grid toc-intro">')
            toc_open('ch', '<div class="toc-card">')
        elif toc[-1][0] == 3:
            toc_open('subs', '<div class="toc-subs">')

    def close_lists(to_level=-1):
        while list_stack and list_stack[-1][0] > to_level:
            lvl, ordered = list_stack.pop()
            out.append('</ol>' if ordered else '</ul>')
            if list_stack:
                out.append('</li>')

    hero_open, hero_close = hero_parts(doc)
    in_hero = hero_done = in_sheet = False

    for b in doc.blocks:
        bid = b['block']
        idattr = f' id="{esc(b["id"])}"' if b['id'] else f' id="{esc(bid)}"'
        alias = f' data-alias="{b["alias"]}"' if b.get('alias') else ''
        if b['type'] != 'li' and list_stack:
            close_lists()
        if toc and not b.get('toc'):
            toc_close(0)
        if b.get('toc'):
            toc_enter(b['toc'])
        if in_hero and not (b['type'] == 'p' and b.get('cls') == 'subtitle'):
            out.append(hero_close); in_hero = False
        if b['type'] == 'h' and b['level'] == 1 and not hero_done:
            out.append(hero_open); in_hero = hero_done = True
        if b.get('cls') == 'pagemark':
            if in_sheet:
                out.append('</section>')
            out.append('<section class="sheet">'); in_sheet = True
        if b['type'] == 'tr':
            if b.get('table_start'):
                out.append('<div class="tablewrap"><table>')
                in_table = True
            tag = 'th' if b.get('header') else 'td'
            cells = ''.join(f'<{tag}>{c}</{tag}>' for c in b['cells'])
            out.append(f'<tr class="blk" tabindex="0" data-block="{bid}"{idattr}>{cells}</tr>')
            if b.get('table_end'):
                out.append('</table></div>'); in_table = False
            continue
        if b['type'] == 'h':
            cls = ' ' + b['cls'] if b.get('cls') else ''
            out.append(f'<h{b["level"]} class="blk{cls}" tabindex="0" data-block="{bid}"{idattr}{alias}>{b["html"]}</h{b["level"]}>')
        elif b['type'] == 'p':
            cls = ' ' + b['cls'] if b.get('cls') else ''
            out.append(f'<p class="blk{cls}" tabindex="0" data-block="{bid}"{idattr}>{b["html"]}</p>')
            if b.get('toc') == 'part':
                toc_open('grid', '<div class="toc-grid">')
        elif b['type'] == 'li':
            lvl = b['level']; ordered = b.get('ordered', False)
            if not list_stack:
                out.append('<ol>' if ordered else '<ul>'); list_stack.append((lvl, ordered))
            elif lvl > list_stack[-1][0]:
                # nest inside the previous li: reopen it
                if out and out[-1].endswith('</li>'):
                    out[-1] = out[-1][:-5]
                out.append('<ol>' if ordered else '<ul>'); list_stack.append((lvl, ordered))
            elif lvl < list_stack[-1][0]:
                close_lists(lvl)
                if not list_stack:
                    out.append('<ol>' if ordered else '<ul>'); list_stack.append((lvl, ordered))
            out.append(f'<li class="blk" tabindex="0" data-block="{bid}"{idattr}>{b["html"]}</li>')
    close_lists()
    toc_close(0)
    if in_hero:
        out.append(hero_close)
    if in_sheet:
        out.append('</section>')
    return '\n'.join(out)

def nav_html(current):
    items = []
    for d in DOCS:
        cur = ' aria-current="page"' if d['id'] == current else ''
        items.append(f'<li><a href="{d["id"]}.html"{cur}>{esc(d["nav"])}</a></li>')
    home_cur = ' aria-current="page"' if current == 'index' else ''
    return f'''<header class="topbar">
  <nav aria-label="ניווט בין המסמכים">
    <a class="brand" href="index.html"{home_cur} title="דף הכניסה"><img src="{asset('logo.png')}" alt="rep creative" width="137" height="30"><span class="vh">דף הכניסה</span></a>
    <ul class="doclinks">{''.join(items)}</ul>
    <div class="tools">
      <span class="count" id="navCount" aria-live="polite"></span>
      <button type="button" id="outlineBtn" class="outline-btn" aria-label="ניווט במסמך" aria-controls="outline" aria-expanded="false" hidden><svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M3 5h14M3 10h10M3 15h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/></svg><span>ניווט במסמך</span></button>
      <button type="button" id="exportBtn">ייצוא הערות</button>
      <button type="button" id="importBtn">ייבוא הערות</button>
      <input type="file" id="importFile" accept="application/json,.json" hidden aria-label="בחירת קובץ הערות לייבוא">
      <button type="button" id="lockBtn" title="נעילת המסמכים בלשונית זו">נעילה</button>
    </div>
  </nav>
  <div class="progress" aria-hidden="true"><span id="progressBar"></span></div>
</header>'''

def asset(name):
    path = os.path.join(SITE, 'assets', name)
    h = hashlib.sha256(open(path, 'rb').read()).hexdigest()[:10] if os.path.exists(path) else '0'
    return f'assets/{name}?v={h}'

def docs_script():
    return 'window.DOCS = ' + json.dumps([dict(id=m['id'], title=m['title'], nav=m['nav']) for m in DOCS], ensure_ascii=False) + ';'

def csp_hash(script_text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(script_text.encode('utf-8')).digest()).decode('ascii') + "'"

def csp_meta():
    hashes = [csp_hash(docs_script())]
    policy = ("default-src 'none'; script-src 'self' " + ' '.join(hashes) + " https://www.clarity.ms https://scripts.clarity.ms; "
              "style-src 'self'; img-src 'self' data: https://*.clarity.ms https://c.bing.com; connect-src https://*.clarity.ms https://c.bing.com; "
              "font-src 'self'; base-uri 'none'; form-action 'self'; frame-src 'none'; object-src 'none'; manifest-src 'none'")
    return '<meta http-equiv="Content-Security-Policy" content="' + policy + '">'

def page(title, body, docid, extra_head=''):
    return f'''<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{asset('style.css')}">{extra_head}
</head>
<body data-doc="{docid}" data-title="{html.escape(title, quote=True)}">
<a class="skip" href="#main">דלג לתוכן</a>
{nav_html(docid)}
<main id="main" class="{'doc' if docid != 'index' else 'home'}">
{body}
</main>
<aside id="outline" class="outline" aria-labelledby="outline-h" hidden>
  <div class="outline-head"><h2 id="outline-h">ניווט במסמך</h2><button type="button" class="close" id="outlineClose" aria-label="סגירת הניווט">×</button></div>
  <ol id="outlineList"></ol>
</aside>
<div id="outlineScrim" class="outline-scrim" hidden></div>
<footer class="sitefoot"><img src="{asset('logo-symbol.png')}" alt="" width="40" height="19"><span>rep creative</span></footer>
<div id="floats">
<ul id="returnTrail" class="return-trail" aria-label="כל התחנות שאפשר לחזור אליהן" hidden></ul>
<div id="returnDoc" class="float-return" hidden>
  <button type="button" id="returnDocBtn"></button>
  <button type="button" class="more" id="returnDocMore" aria-label="הצגת כל הצעדים אחורה" aria-expanded="false" aria-controls="returnTrail" hidden>▲</button>
  <button type="button" class="close" id="returnDocClose" aria-label="סגירת כפתור החזרה">×</button>
</div>
<div id="returnPos" class="float-return" hidden>
  <button type="button" id="returnPosBtn">חזרה למקום שבו קראתי</button>
  <button type="button" class="close" id="returnPosClose" aria-label="סגירת כפתור החזרה">×</button>
</div>
</div>
<div id="status" class="status" role="status" aria-live="polite"></div>
<script>{docs_script()}</script>
<script src="{asset('app.js')}"></script>
</body>
</html>
'''

def sources_html(doc, docs):
    if not doc.refs:
        return '<section class="sources" aria-labelledby="sources-h"><h2 id="sources-h">מקורות במסמך זה</h2><p>לא זוהו הפניות למסמכים אחרים.</p></section>'
    groups = collections.OrderedDict()
    for text, tdoc, anchor, sure in doc.refs:
        groups.setdefault(tdoc, collections.OrderedDict())
        key = anchor or ''
        g = groups[tdoc].setdefault(key, dict(count=0, sure=True, texts=collections.Counter()))
        g['count'] += 1; g['sure'] = g['sure'] and sure; g['texts'][text] += 1
    parts = ['<section class="sources" aria-labelledby="sources-h"><h2 id="sources-h">מקורות במסמך זה</h2>']
    for tdoc in sorted(groups):
        meta = DOC_BY_ID[tdoc]; target = docs[tdoc]
        parts.append(f'<h3>{esc(meta["title"])}</h3><ul>')
        for anchor, g in groups[tdoc].items():
            label = 'ראש המסמך'
            if anchor:
                for b in target.blocks:
                    if b['id'] == anchor or b.get('alias') == anchor:
                        label = html.unescape(plain(b))[:90]; break
            href = f'{tdoc}.html' + (f'#{anchor}' if anchor else '')
            unsure = ' <span class="unsure">(הפניה לא ודאית)</span>' if not g['sure'] else ''
            cnt = 'הפניה אחת' if g['count'] == 1 else str(g['count']) + ' הפניות'
            parts.append(f'<li><a href="{href}" data-xref="1">{esc(label)}</a> <span class="muted">({cnt})</span>{unsure}</li>')
        parts.append('</ul>')
    parts.append('</section>')
    return '\n'.join(parts)

# ------------------------------ encryption --------------------------------

def load_password():
    if os.path.exists(PASSWORD_FILE):
        pwd = open(PASSWORD_FILE, encoding='utf-8').read().strip()
        if pwd:
            return pwd, False
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+?'
    pwd = ''.join(secrets.choice(alphabet) for _ in range(32))
    with open(PASSWORD_FILE, 'w', encoding='utf-8') as f:
        f.write(pwd + '\n')
    os.chmod(PASSWORD_FILE, stat.S_IRUSR | stat.S_IWUSR)
    if os.path.exists(SALT_FILE):
        os.remove(SALT_FILE)      # a new password gets a new salt
    return pwd, True

def load_salt():
    """The salt stays fixed across builds so keys cached in readers' browsers keep working."""
    if os.path.exists(SALT_FILE):
        h = open(SALT_FILE, encoding='utf-8').read().strip()
        if re.fullmatch(r'[0-9a-f]{32}', h):
            return bytes.fromhex(h)
    salt = os.urandom(16)
    with open(SALT_FILE, 'w', encoding='utf-8') as f:
        f.write(salt.hex() + '\n')
    os.chmod(SALT_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return salt

def aes_cbc_encrypt(plain, key, iv):
    r = subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-K', key.hex(), '-iv', iv.hex(), '-nosalt'],
                       input=plain, capture_output=True, check=True)
    return r.stdout

def clarity_id():
    pid = (CONFIG.get('clarity_project_id') or '').strip()
    return pid if re.fullmatch(r'[A-Za-z0-9]+', pid) else ''

def clarity_inline():
    pid = clarity_id()
    if not pid:
        return ''
    return ('(function(c,l,a,r,i,t,y){c[a]=c[a]||function(){(c[a].q=c[a].q||[]).push(arguments)};'
            't=l.createElement(r);t.async=1;t.src="https://www.clarity.ms/tag/"+i;y=l.getElementsByTagName(r)[0];'
            'y.parentNode.insertBefore(t,y);})(window, document, "clarity", "script", "' + pid + '");')

def clarity_snippet():
    cl = clarity_inline()
    return ('\n<script>' + cl + '</script>') if cl else ''

def encrypt_page(full_html, key, salt_hex, iterations):
    title = re.search(r'<title>(.*?)</title>', full_html, flags=re.S).group(1)
    m = re.search(r'<body data-doc="([^"]*)" data-title="([^"]*)">(.*)</body>', full_html, flags=re.S)
    payload = json.dumps(dict(doc=m.group(1), title=html.unescape(m.group(2)), html=m.group(3)), ensure_ascii=False)
    iv = os.urandom(16)
    ct = aes_cbc_encrypt(('OK::' + payload).encode('utf-8'), key, iv)
    b64 = base64.b64encode(ct).decode('ascii')
    b64 = '\n'.join(b64[i:i + 120] for i in range(0, len(b64), 120))
    return f'''<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
{csp_meta()}
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<link rel="icon" type="image/png" sizes="32x32" href="{asset('favicon-32.png')}">
<link rel="icon" type="image/png" sizes="192x192" href="{asset('favicon-192.png')}">
<link rel="apple-touch-icon" href="{asset('apple-touch-icon.png')}">
<meta name="theme-color" content="#f7f5f1">
<title>מסמכי הפרויקט</title>
<link rel="stylesheet" href="{asset('style.css')}">
</head>
<body data-doc="gate" data-title="">
<main class="gate" id="gate" data-clarity-mask="true">
  <img class="logo" src="{asset('logo.png')}" alt="rep creative" width="640" height="140">
  <form id="gateForm" aria-labelledby="gate-h">
    <h1 id="gate-h">מסמכי הפרויקט</h1>
    <p>הדפים מוצפנים. הזינו את הסיסמה שקיבלתם כדי לפתוח אותם בלשונית זו.</p>
    <label for="gatePwd">סיסמה</label>
    <input id="gatePwd" type="password" autocomplete="current-password" autocapitalize="off" spellcheck="false" required autofocus>
    <label class="check"><input type="checkbox" id="gateRemember"> זכור אותי במכשיר זה ל-8 שעות</label>
    <div class="row"><button type="submit" class="primary" id="gateBtn">כניסה</button><span id="gateMsg" role="status" aria-live="polite"></span></div>
    <p class="err" id="gateErr" role="alert"></p>
  </form>
  <p class="gate-foot">גישה מאובטחת · הצפנת AES-256</p>
</main>
<noscript><p class="gate-noscript">נדרש JavaScript כדי לפענח את המסמכים.</p></noscript>
<script type="text/plain" id="payload" data-iv="{iv.hex()}" data-salt="{salt_hex}" data-iter="{iterations}" data-clarity="{clarity_id()}">
{b64}
</script>
<script src="{asset('gate.js')}"></script>
</body>
</html>
'''

# Columns removed from specific documents at the owner's request (29.10.2026).
DROP_COLUMNS = {'05-client-response': ['התגובה שלי']}

def drop_columns(doc):
    names = DROP_COLUMNS.get(doc.id)
    if not names:
        return 0
    removed = 0; drop_idx = None
    for b in doc.blocks:
        if b['type'] != 'tr':
            drop_idx = None
            continue
        if b.get('table_start'):
            drop_idx = None
            for i, c in enumerate(b['cells']):
                if html.unescape(re.sub(r'<[^>]+>', '', c)).strip() in names:
                    drop_idx = i
                    removed += 1
                    break
        if drop_idx is not None and drop_idx < len(b['cells']):
            del b['cells'][drop_idx]
    return removed

def build():
    docs = {}
    for meta in DOCS:
        d = Doc(meta)
        {'docx': convert_docx, 'md': convert_md, 'pdf': convert_pdf}[meta['kind']](d)
        n = drop_columns(d)
        if n:
            print(f'{d.id}: removed column(s) {DROP_COLUMNS[d.id]} from {n} table(s)')
        assign_ids(d)
        docs[d.id] = d
    for d in docs.values():
        apply_links(d, docs)
        decorate(d)
    report = dict(docs={}, refs=[], unsure=[])
    pages = {}
    for d in docs.values():
        body = f'<article class="document" aria-label="{esc(d.meta["title"])}">\n{render_blocks(d)}\n</article>\n{sources_html(d, docs)}'
        pages[d.id] = page(d.meta['title'], body, d.id)
        c = collections.Counter(b['type'] for b in d.blocks)
        hc = collections.Counter(b['level'] for b in d.blocks if b['type'] == 'h')
        report['docs'][d.id] = dict(file=d.meta['file'], blocks=len(d.blocks), headings=dict(hc), paragraphs=c['p'], list_items=c['li'], table_rows=c['tr'], internal_links=d.internal_links, dead_links=getattr(d,'dead_links',0), cross_refs=len(d.refs))
        for text, tdoc, anchor, sure in d.refs:
            report['refs'].append(dict(src=d.id, text=text, target=tdoc, anchor=anchor, sure=sure))
        for text, tdoc, why in d.unsure:
            report['unsure'].append(dict(src=d.id, text=text, target=tdoc, why=why))
    # index
    items = []
    for meta in DOCS:
        items.append(f'''<li class="doccard"><a href="{meta['id']}.html"><span class="num">{meta['id'][:2]}</span><span class="t">{esc(meta['title'])}</span></a>
<p>{esc(meta['desc'])}</p><span class="count" data-doc-count="{meta['id']}"></span></li>''')
    body = f'''<header class="hero">
<img class="mark" src="{asset('logo-symbol.png')}" alt="" width="480" height="229">
<p class="eyebrow">מסמכי הפרויקט</p>
<h1>מסמכי פלטפורמת התיירות הלאומית</h1>
<p class="lead">חמשת מסמכי הפרויקט בגרסת קריאה מקושרת: כל הפניה בין מסמכים היא קישור, ואפשר להוסיף הערות על כל פסקה, כותרת או שורת טבלה. ההערות נשמרות בדפדפן הזה בלבד, וניתן לייצא ולייבא אותן כקובץ.</p>
</header>
<ol class="doclist">
{''.join(items)}
</ol>
<p class="muted" id="totalCount"></p>'''
    pages['index'] = page('מסמכי פלטפורמת התיירות הלאומית', body, 'index')
    # encrypt every page with a key derived from the site password
    pwd, generated = load_password()
    iterations = int(CONFIG.get('pbkdf2_iterations') or 600000)
    salt = load_salt()
    key = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), salt, iterations, dklen=32)
    for name, full in pages.items():
        open(os.path.join(SITE, name + '.html'), 'w', encoding='utf-8').write(encrypt_page(full, key, salt.hex(), iterations))
    old = os.path.join(SITE, 'assets', 'docs.js')
    if os.path.exists(old):
        os.remove(old)
    print('pages encrypted:', len(pages), '| password file:', PASSWORD_FILE, '(new)' if generated else '(existing)')
    json.dump(report, open(os.path.join(BUILD, 'report.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for k, v in report['docs'].items():
        print(k, v)
    print('cross refs:', len(report['refs']), 'unsure:', len(report['unsure']))

if __name__ == '__main__':
    build()
