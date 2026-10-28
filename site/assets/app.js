/* Review site behaviour: cross-document return, in-document return, comments.
   No network, no dependencies. Works from file:// and from a static server. */
(function () {
  'use strict';
  var STORE_KEY = 'tourism-docs-comments';
  var AUTHOR_KEY = 'tourism-docs-author';
  var RET_KEY = 'tourism-docs-return-stack';
  var RESTORE_KEY = 'tourism-docs-restore';

  var body = document.body;
  var DOC = body.getAttribute('data-doc');
  var TITLE = body.getAttribute('data-title');
  var isDocPage = DOC !== 'index';
  var docTitles = {}, DOC_IDS = [];
  (window.DOCS || []).forEach(function (d) { docTitles[d.id] = d.title; DOC_IDS.push(d.id); });
  var ID_RE = /^[\w\u0590-\u05ff:.-]{1,120}$/;
  var MAX_IMPORT_BYTES = 2000000, MAX_COMMENTS = 5000;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function isoLocal(d) {
    var off = -d.getTimezoneOffset(), s = off >= 0 ? '+' : '-', a = Math.abs(off);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + s + pad(Math.floor(a / 60)) + ':' + pad(a % 60);
  }
  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return iso || '';
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function uid() { return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10); }
  function hashText(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(16);
  }
  function blockText(blk) {
    var clone = blk.cloneNode(true);
    $all('.cmts, .cmt-row, #blkTools', clone).forEach(function (n) { n.parentNode.removeChild(n); });
    return clone.textContent.replace(/\s+/g, ' ').trim();
  }
  function byId(id) {
    if (!id || !ID_RE.test(id)) return null;
    var el = document.getElementById(id);
    if (el) return el;
    try { return $('[data-alias="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]'); } catch (e) { return null; }
  }
  function cleanComment(c) {
    if (!c || typeof c !== 'object') return null;
    var str = function (v, max) { return typeof v === 'string' && v.length <= max ? v : null; };
    var id = str(c.id, 64), doc = str(c.doc, 40), block = str(c.block, 80), author = str(c.author, 100), text = str(c.text, 5000);
    var createdAt = str(c.createdAt, 40);
    if (!id || !/^[\w-]+$/.test(id) || DOC_IDS.indexOf(doc) === -1 || !block || block.indexOf(doc + ':') !== 0 || !/^[\w-]+:\d+$/.test(block)) return null;
    if (author === null || text === null || !createdAt) return null;
    return {
      id: id, doc: doc, block: block, author: author, text: text, createdAt: createdAt,
      updatedAt: str(c.updatedAt, 40) || null,
      blockHash: str(c.blockHash, 16) || undefined
    };
  }
  function blockById(bid) {
    var els = $all('.blk');
    for (var i = 0; i < els.length; i++) if (els[i].getAttribute('data-block') === bid) return els[i];
    return null;
  }
  var statusTimer;
  function status(msg) {
    var el = $('#status');
    if (!el) return;
    el.textContent = msg; el.classList.add('show');
    clearTimeout(statusTimer);
    statusTimer = setTimeout(function () { el.classList.remove('show'); }, 2800);
  }
  function flash(el) {
    if (!el) return;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(function () { el.classList.remove('flash'); }, 1900);
  }
  function safeJSON(s, fallback) { try { return JSON.parse(s); } catch (e) { return fallback; } }

  /* ---------------- storage ---------------- */
  function load() {
    var data = safeJSON(localStorage.getItem(STORE_KEY), null);
    if (!data || !Array.isArray(data.comments)) data = { version: 1, comments: [] };
    return data;
  }
  function save(data) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); }
    catch (e) { status('לא ניתן לשמור: אחסון הדפדפן מלא או חסום'); }
  }
  function commentsFor(bid) {
    return load().comments.filter(function (c) { return c.block === bid; })
      .sort(function (a, b) { return (a.createdAt || '').localeCompare(b.createdAt || ''); });
  }
  function countFor(docId) { return load().comments.filter(function (c) { return c.doc === docId; }).length; }

  /* ---------------- counters ---------------- */
  function updateCounts() {
    var data = load();
    var nav = $('#navCount');
    if (nav) {
      if (isDocPage) {
        var n = data.comments.filter(function (c) { return c.doc === DOC; }).length;
        nav.textContent = 'הערות במסמך זה: ' + n + ' (סה"כ ' + data.comments.length + ')';
      } else {
        nav.textContent = 'סה"כ הערות: ' + data.comments.length;
      }
    }
    $all('[data-doc-count]').forEach(function (el) {
      var n = countFor(el.getAttribute('data-doc-count'));
      el.textContent = n ? (n === 1 ? 'הערה אחת' : n + ' הערות') : '';
    });
    var total = $('#totalCount');
    if (total) total.textContent = data.comments.length ? 'סה"כ ' + data.comments.length + ' הערות בכל המסמכים.' : 'עדיין אין הערות.';
  }

  /* ---------------- export / import ---------------- */
  function exportComments() {
    var data = load();
    var d = new Date();
    var name = 'comments-' + d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + '.json';
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 500);
    status('יוצאו ' + data.comments.length + ' הערות לקובץ ' + name);
  }
  function importComments(file) {
    if (file.size > MAX_IMPORT_BYTES) { status('הקובץ גדול מדי לייבוא'); return; }
    var reader = new FileReader();
    reader.onload = function () {
      var incoming = safeJSON(reader.result, null);
      if (!incoming || incoming.version !== 1 || !Array.isArray(incoming.comments)) { status('הקובץ אינו קובץ הערות תקין'); return; }
      var data = load();
      var byIdMap = {};
      data.comments.forEach(function (c) { byIdMap[c.id] = c; });
      var added = 0, updated = 0, rejected = 0;
      incoming.comments.slice(0, MAX_COMMENTS).forEach(function (raw) {
        var c = cleanComment(raw);
        if (!c) { rejected++; return; }
        var cur = byIdMap[c.id];
        if (!cur) { if (data.comments.length >= MAX_COMMENTS) { rejected++; return; } data.comments.push(c); byIdMap[c.id] = c; added++; return; }
        var a = cur.updatedAt || cur.createdAt || '', b = c.updatedAt || c.createdAt || '';
        if (b > a) { Object.assign(cur, c); updated++; }
      });
      save(data);
      renderAll();
      status('הייבוא הושלם: ' + added + ' הערות חדשות, ' + updated + ' עודכנו' + (rejected ? ', ' + rejected + ' נדחו' : '') + ', ' + data.comments.length + ' סה"כ');
    };
    reader.readAsText(file);
  }

  /* ---------------- block selection ---------------- */
  var selected = null;
  var tools = null;
  function ensureTools() {
    if (tools) return tools;
    tools = document.createElement('div');
    tools.id = 'blkTools';
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = 'הוסף הערה';
    b.addEventListener('click', function () { if (selected) openForm(selected, null); });
    tools.appendChild(b);
    document.body.appendChild(tools);
    return tools;
  }
  function positionTools() {
    if (!selected || !tools) return;
    var r = selected.getBoundingClientRect();
    var top = r.top + window.scrollY - 38;
    if (top < window.scrollY + 70) top = r.bottom + window.scrollY + 4;
    tools.style.top = top + 'px';
    var docW = document.documentElement.clientWidth;
    tools.style.right = Math.max(8, docW - r.right) + 'px';
    tools.style.left = 'auto';
  }
  function clearSelect() {
    if (selected) selected.classList.remove('selected');
    selected = null;
    if (tools) tools.remove(); tools = null;
  }
  function select(blk, viaKeyboard) {
    clearSelect();
    selected = blk; blk.classList.add('selected');
    ensureTools(); positionTools();
    if (viaKeyboard) $('button', tools).focus();
  }
  function toggleSelect(blk, viaKeyboard) {
    if (selected === blk) { clearSelect(); if (viaKeyboard) blk.focus(); }
    else select(blk, viaKeyboard);
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('a, button, input, textarea, select, label, .cmts, .cmt-row, #blkTools, .topbar, #floats')) return;
    var blk = e.target.closest('.blk');
    if (blk && window.getSelection && String(window.getSelection()).length) return;
    if (blk) toggleSelect(blk, false); else clearSelect();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (selected) { var s = selected; clearSelect(); s.focus(); }
      var openForms = $all('.cmt-form');
      if (openForms.length) { closeForms(); }
      return;
    }
    if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('blk')) {
      e.preventDefault();
      toggleSelect(e.target, true);
    }
  });
  window.addEventListener('scroll', positionTools, { passive: true });
  window.addEventListener('resize', positionTools);

  /* ---------------- comments rendering ---------------- */
  function hostFor(blk, create) {
    if (blk.tagName === 'TR') {
      var next = blk.nextElementSibling;
      if (next && next.classList.contains('cmt-row')) return $('.cmts', next);
      if (!create) return null;
      var tr = document.createElement('tr'); tr.className = 'cmt-row';
      var td = document.createElement('td'); td.colSpan = blk.children.length || 1;
      var div = document.createElement('div'); div.className = 'cmts';
      td.appendChild(div); tr.appendChild(td);
      blk.parentNode.insertBefore(tr, blk.nextSibling);
      return div;
    }
    if (blk.tagName === 'LI') {
      var inner = blk.querySelector(':scope > .cmts');
      if (inner) return inner;
      if (!create) return null;
      var d = document.createElement('div'); d.className = 'cmts';
      blk.appendChild(d); return d;
    }
    var sib = blk.nextElementSibling;
    if (sib && sib.classList.contains('cmts')) return sib;
    if (!create) return null;
    var el = document.createElement('div'); el.className = 'cmts';
    blk.parentNode.insertBefore(el, blk.nextSibling);
    return el;
  }
  function removeHostIfEmpty(blk) {
    var host = hostFor(blk, false);
    if (host && !host.children.length) {
      if (blk.tagName === 'TR') host.parentNode.parentNode.remove(); else host.remove();
    }
  }
  function renderBlock(blk) {
    var bid = blk.getAttribute('data-block');
    var list = commentsFor(bid);
    var host = hostFor(blk, false);
    if (host) $all('.cmt', host).forEach(function (n) { n.remove(); });
    if (list.length) {
      host = hostFor(blk, true);
      var form = $('.cmt-form', host);
      var curHash = hashText(blockText(blk));
      list.forEach(function (c) {
        var item = document.createElement('div'); item.className = 'cmt'; item.setAttribute('data-id', c.id);
        var meta = document.createElement('div'); meta.className = 'meta';
        var au = document.createElement('span'); au.className = 'author'; au.textContent = c.author || '';
        var dt = document.createElement('span'); dt.textContent = fmtDate(c.createdAt);
        meta.appendChild(au); meta.appendChild(dt);
        if (c.updatedAt) { var up = document.createElement('span'); up.textContent = '(נערך ' + fmtDate(c.updatedAt) + ')'; meta.appendChild(up); }
        if (c.blockHash && c.blockHash !== curHash) { var ch = document.createElement('span'); ch.className = 'changed'; ch.textContent = 'הפסקה השתנתה'; meta.appendChild(ch); }
        var text = document.createElement('div'); text.className = 'text'; text.textContent = c.text || '';
        var actions = document.createElement('div'); actions.className = 'actions';
        var eb = document.createElement('button'); eb.type = 'button'; eb.textContent = 'עריכה';
        eb.setAttribute('aria-label', 'עריכת ההערה של ' + (c.author || ''));
        eb.addEventListener('click', function () { openForm(blk, c); });
        var db = document.createElement('button'); db.type = 'button'; db.textContent = 'מחיקה'; db.className = 'danger';
        db.setAttribute('aria-label', 'מחיקת ההערה של ' + (c.author || ''));
        db.addEventListener('click', function () {
          if (!confirm('למחוק את ההערה של ' + (c.author || '') + '?')) return;
          var data = load();
          data.comments = data.comments.filter(function (x) { return x.id !== c.id; });
          save(data); renderBlock(blk); updateCounts(); status('ההערה נמחקה');
          blk.focus();
        });
        actions.appendChild(eb); actions.appendChild(db);
        item.appendChild(meta); item.appendChild(text); item.appendChild(actions);
        if (form) host.insertBefore(item, form); else host.appendChild(item);
      });
      blk.classList.add('has-comments');
      blk.setAttribute('data-count', String(list.length));
      if (blk.tagName === 'TR' && blk.firstElementChild) blk.firstElementChild.setAttribute('data-count', String(list.length));
    } else {
      blk.classList.remove('has-comments'); blk.removeAttribute('data-count');
      if (blk.tagName === 'TR' && blk.firstElementChild) blk.firstElementChild.removeAttribute('data-count');
      removeHostIfEmpty(blk);
    }
  }
  function closeForms() {
    $all('.cmt-form').forEach(function (f) {
      var blk = f.__blk;
      f.remove();
      if (blk) { removeHostIfEmpty(blk); }
    });
  }
  function openForm(blk, existing) {
    closeForms();
    clearSelect();
    var host = hostFor(blk, true);
    var form = document.createElement('form'); form.className = 'cmt-form'; form.__blk = blk;
    form.setAttribute('aria-label', existing ? 'עריכת הערה' : 'הערה חדשה');
    var fid = uid();
    form.innerHTML =
      '<label for="n' + fid + '">שם (חובה)</label>' +
      '<input id="n' + fid + '" type="text" name="author" autocomplete="name" required>' +
      '<label for="t' + fid + '">הערה</label>' +
      '<textarea id="t' + fid + '" name="text" required></textarea>' +
      '<div class="row"><button type="submit" class="primary">' + (existing ? 'שמירת השינויים' : 'שמירת ההערה') + '</button>' +
      '<button type="button" class="cancel">ביטול</button><span class="err" role="alert"></span></div>';
    var nameIn = $('input', form), textIn = $('textarea', form), err = $('.err', form);
    nameIn.value = existing ? (existing.author || '') : (localStorage.getItem(AUTHOR_KEY) || '');
    textIn.value = existing ? (existing.text || '') : '';
    $('.cancel', form).addEventListener('click', function () { form.remove(); removeHostIfEmpty(blk); blk.focus(); });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var author = nameIn.value.trim(), text = textIn.value.trim();
      if (!author) { err.textContent = 'יש להזין שם'; nameIn.focus(); return; }
      if (!text) { err.textContent = 'יש להזין טקסט להערה'; textIn.focus(); return; }
      localStorage.setItem(AUTHOR_KEY, author);
      var data = load();
      if (existing) {
        var c = data.comments.filter(function (x) { return x.id === existing.id; })[0];
        if (c) { c.author = author; c.text = text; c.updatedAt = isoLocal(new Date()); }
      } else {
        data.comments.push({
          id: uid(), doc: DOC, block: blk.getAttribute('data-block'), author: author, text: text,
          createdAt: isoLocal(new Date()), updatedAt: null, blockHash: hashText(blockText(blk))
        });
      }
      save(data);
      form.remove();
      renderBlock(blk); updateCounts();
      status(existing ? 'ההערה עודכנה' : 'ההערה נשמרה');
      blk.focus();
    });
    host.appendChild(form);
    (nameIn.value ? textIn : nameIn).focus();
  }
  function renderAll() {
    if (!isDocPage) { updateCounts(); return; }
    var data = load();
    var touched = {};
    $all('.blk.has-comments').forEach(function (b) { touched[b.getAttribute('data-block')] = b; });
    data.comments.forEach(function (c) { if (c.doc === DOC) { var b = blockById(c.block); if (b) touched[c.block] = b; } });
    Object.keys(touched).forEach(function (k) { renderBlock(touched[k]); });
    // comments whose block no longer exists in this document
    var orphans = data.comments.filter(function (c) { return c.doc === DOC && !blockById(c.block); });
    var old = $('#orphans'); if (old) old.remove();
    if (orphans.length) {
      var sec = document.createElement('section'); sec.id = 'orphans'; sec.className = 'sources';
      sec.innerHTML = '<h2>הערות על יחידות שלא נמצאו במסמך</h2>';
      orphans.forEach(function (c) {
        var d = document.createElement('div'); d.className = 'cmt';
        d.innerHTML = '<div class="meta"><span class="author"></span><span></span><span class="changed">הפסקה השתנתה</span></div><div class="text"></div>';
        $('.author', d).textContent = c.author || ''; $('.meta span:nth-child(2)', d).textContent = fmtDate(c.createdAt) + ' · ' + c.block;
        $('.text', d).textContent = c.text || '';
        sec.appendChild(d);
      });
      $('#main').appendChild(sec);
    }
    updateCounts();
  }

  /* ---------------- cross-document return ---------------- */
  function retStack() { return safeJSON(sessionStorage.getItem(RET_KEY), []) || []; }
  function setRetStack(s) { sessionStorage.setItem(RET_KEY, JSON.stringify(s)); }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-xref]');
    if (!a || !isDocPage) return;
    var href = a.getAttribute('href') || '';
    var toDoc = href.split('#')[0].replace(/\.html$/, '');
    var blk = a.closest('.blk');
    var s = retStack();
    s.push({ fromDoc: DOC, fromTitle: TITLE, fromAnchor: blk ? blk.getAttribute('data-block') : null, scrollY: window.scrollY, toDoc: toDoc });
    if (s.length > 20) s.shift();
    setRetStack(s);
  });
  function showReturnDoc() {
    var box = $('#returnDoc'); if (!box) return;
    var s = retStack(); var top = s[s.length - 1];
    if (!top || top.toDoc !== DOC || DOC_IDS.indexOf(top.fromDoc) === -1) { box.hidden = true; return; }
    $('#returnDocBtn').textContent = 'חזרה ל-' + (top.fromTitle || docTitles[top.fromDoc] || top.fromDoc);
    box.hidden = false;
    $('#returnDocBtn').onclick = function () {
      var st = retStack(); var t = st.pop(); setRetStack(st);
      sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ doc: t.fromDoc, anchor: t.fromAnchor, scrollY: t.scrollY }));
      var anchor = t.fromAnchor && ID_RE.test(t.fromAnchor) ? '#' + encodeURIComponent(t.fromAnchor) : '';
      location.href = t.fromDoc + '.html' + anchor;
    };
    $('#returnDocClose').onclick = function () { var st = retStack(); st.pop(); setRetStack(st); box.hidden = true; };
  }
  function restoreIfNeeded() {
    var r = safeJSON(sessionStorage.getItem(RESTORE_KEY), null);
    if (!r || r.doc !== DOC) return false;
    sessionStorage.removeItem(RESTORE_KEY);
    var blk = r.anchor ? blockById(r.anchor) : null;
    function go() {
      window.scrollTo(0, r.scrollY || 0);
      if (blk) { flash(blk); blk.focus({ preventScroll: true }); }
    }
    go(); requestAnimationFrame(go); setTimeout(go, 60);
    return true;
  }

  /* ---------------- in-document return ---------------- */
  var savedPos = null;
  function showReturnPos() { var b = $('#returnPos'); if (b) b.hidden = false; }
  function hideReturnPos() { var b = $('#returnPos'); if (b) b.hidden = true; }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[href^="#"]');
    if (!a || a.closest('.topbar') || a.getAttribute('href') === '#main') return;
    var id = decodeURIComponent(a.getAttribute('href').slice(1));
    if (!id) { e.preventDefault(); return; }
    var el = byId(id);
    if (!el) return;
    e.preventDefault();
    savedPos = window.scrollY;
    history.pushState(null, '', '#' + encodeURIComponent(id));
    el.scrollIntoView({ block: 'start' });
    flash(el);
    if (el.hasAttribute('tabindex')) el.focus({ preventScroll: true });
    showReturnPos();
  });
  var rp = $('#returnPosBtn'), rpc = $('#returnPosClose');
  if (rp) rp.addEventListener('click', function () {
    if (savedPos !== null) window.scrollTo({ top: savedPos });
    hideReturnPos();
  });
  if (rpc) rpc.addEventListener('click', hideReturnPos);

  function jumpToHash() {
    if (!location.hash) return;
    var id = decodeURIComponent(location.hash.slice(1));
    var el = byId(id);
    if (!el) return;
    el.scrollIntoView({ block: 'start' });
    flash(el);
    if (el.hasAttribute('tabindex')) el.focus({ preventScroll: true });
  }

  /* ---------------- init ---------------- */
  var lock = $('#lockBtn');
  if (lock) lock.addEventListener('click', function () {
    try { sessionStorage.removeItem('tourism-docs-key'); } catch (e) {}
    try { localStorage.removeItem('tourism-docs-key-persist'); } catch (e) {}
    location.href = location.pathname;
  });
  var ex = $('#exportBtn'), im = $('#importBtn'), imf = $('#importFile');
  if (ex) ex.addEventListener('click', exportComments);
  if (im && imf) {
    im.addEventListener('click', function () { imf.value = ''; imf.click(); });
    imf.addEventListener('change', function () { if (imf.files && imf.files[0]) importComments(imf.files[0]); });
  }
  renderAll();
  if (isDocPage) {
    showReturnDoc();
    if (!restoreIfNeeded()) jumpToHash();
  }
  window.addEventListener('storage', function (e) { if (e.key === STORE_KEY) renderAll(); });
})();
