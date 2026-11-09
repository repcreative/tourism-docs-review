/* Review site behaviour: cross-document return, in-document return, comments.
   Comments are shared through an encrypted store (window.COMMENTS_API); without it,
   or offline, they stay in this browser. No dependencies. */
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
      blockHash: str(c.blockHash, 16) || undefined,
      resolved: c.resolved === true,
      resolvedBy: c.resolved === true ? (str(c.resolvedBy, 100) || '') : null,
      resolvedAt: str(c.resolvedAt, 40) || null
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

  /* ---------------- storage ----------------
     The local copy (localStorage) is a cache of the shared store. Every change is
     marked _sync ('put' or 'del') until the server has it. On the server each comment
     is an AES-GCM blob encrypted with a key derived from the site key, so the server
     never sees comment text, names or which paragraph a comment belongs to. */
  function loadRaw() {
    var data = null;
    try { data = safeJSON(localStorage.getItem(STORE_KEY), null); } catch (e) {}
    if (!data || !Array.isArray(data.comments)) data = { version: 1, comments: [] };
    return data;
  }
  function saveRaw(data) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); return true; }
    catch (e) { status('לא ניתן לשמור: אחסון הדפדפן מלא או חסום'); return false; }
  }
  function load() {
    return { version: 1, comments: loadRaw().comments.filter(function (c) { return c._sync !== 'del'; }) };
  }
  function upsertComment(c) {
    var data = loadRaw();
    c._sync = 'put';
    var i = data.comments.findIndex(function (x) { return x.id === c.id; });
    if (i === -1) data.comments.push(c); else data.comments[i] = c;
    saveRaw(data); queueSync();
  }
  function removeComment(id) {
    var data = loadRaw();
    data.comments.forEach(function (x) { if (x.id === id) x._sync = 'del'; });
    saveRaw(data); queueSync();
  }
  function getComment(id) { return load().comments.filter(function (x) { return x.id === id; })[0] || null; }
  function commentsFor(bid) {
    return load().comments.filter(function (c) { return c.block === bid; })
      .sort(function (a, b) { return (a.createdAt || '').localeCompare(b.createdAt || ''); });
  }
  function docComments(docId) { return load().comments.filter(function (c) { return c.doc === docId; }); }
  function openCount(list) { return list.filter(function (c) { return !c.resolved; }).length; }

  /* ---------------- shared sync ---------------- */
  var API = typeof window.COMMENTS_API === 'string' && /^https:\/\//.test(window.COMMENTS_API) ? window.COMMENTS_API : '';
  var SYNC_EVERY = 60000;
  var cryptoP = null, syncing = false, syncAgain = false, syncTimer = null, syncState = API ? 'pending' : 'off', lastSyncErr = 0;
  function hex2buf(h) { var a = new Uint8Array(h.length / 2); for (var i = 0; i < a.length; i++) a[i] = parseInt(h.substr(i * 2, 2), 16); return a; }
  function buf2hex(b) { return Array.prototype.map.call(new Uint8Array(b), function (x) { return ('0' + x.toString(16)).slice(-2); }).join(''); }
  function buf2b64(b) { var s = '', a = new Uint8Array(b); for (var i = 0; i < a.length; i++) s += String.fromCharCode(a[i]); return btoa(s); }
  function b642buf(s) { var bin = atob(s), a = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a; }
  function siteKeyHex() {
    var k = null;
    try { k = sessionStorage.getItem('tourism-docs-key'); } catch (e) {}
    return k && /^[0-9a-f]{64}$/.test(k) ? k : null;
  }
  function getCrypto() {
    if (cryptoP) return cryptoP;
    var hex = siteKeyHex();
    if (!hex || !window.crypto || !crypto.subtle) return Promise.reject(new Error('no key'));
    var raw = hex2buf(hex), enc = new TextEncoder();
    cryptoP = Promise.all([
      crypto.subtle.importKey('raw', raw, 'HKDF', false, ['deriveKey']).then(function (base) {
        return crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: enc.encode('tourism-comments-enc-v1') },
          base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
      }),
      crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']).then(function (mk) {
        return crypto.subtle.sign('HMAC', mk, enc.encode('tourism-comments-api-v1'));
      }).then(buf2hex)
    ]).then(function (r) { return { key: r[0], token: r[1] }; });
    cryptoP.catch(function () { cryptoP = null; });
    return cryptoP;
  }
  function publicFields(c) {
    var o = {};
    Object.keys(c).forEach(function (k) { if (k.charAt(0) !== '_') o[k] = c[k]; });
    return o;
  }
  function encryptComment(cr, c) {
    var iv = crypto.getRandomValues(new Uint8Array(12));
    return crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv }, cr.key, new TextEncoder().encode(JSON.stringify(publicFields(c))))
      .then(function (ct) { var all = new Uint8Array(12 + ct.byteLength); all.set(iv); all.set(new Uint8Array(ct), 12); return buf2b64(all); });
  }
  function decryptComment(cr, data) {
    var all = b642buf(data);
    return crypto.subtle.decrypt({ name: 'AES-GCM', iv: all.slice(0, 12) }, cr.key, all.slice(12))
      .then(function (pt) { return safeJSON(new TextDecoder().decode(pt), null); });
  }
  var serverEtag = null, serverVersion = '';
  function api(cr, method, body) {
    var headers = { 'Authorization': 'Bearer ' + cr.token };
    if (body) headers['Content-Type'] = 'application/json';
    if (!body && serverEtag) headers['If-None-Match'] = serverEtag;
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, 20000) : null;
    return fetch(API, {
      method: method, cache: 'no-store', credentials: 'omit', headers: headers,
      body: body ? JSON.stringify(body) : undefined, signal: ctl ? ctl.signal : undefined
    }).then(function (r) {
      clearTimeout(timer);
      if (r.status === 304) return null;
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var et = r.headers.get('ETag');
      return r.json().then(function (j) { serverEtag = et || null; return j; });
    });
  }
  function clearFlags(sent) {
    var data = loadRaw();
    data.comments = data.comments.filter(function (x) {
      var f = sent[x.id];
      if (!f || x._sync !== f.sync || x.updatedAt !== f.updatedAt || x.resolvedAt !== f.resolvedAt) return true;
      if (f.sync === 'del') return false;
      delete x._sync; return true;
    });
    saveRaw(data);
  }
  function setSyncState(s) { var changed = s !== syncState; syncState = s; if (changed) updateCounts(); }
  function sync() {
    if (!API) return Promise.resolve();
    if (syncing) { syncAgain = true; return Promise.resolve(); }
    syncing = true;
    var before = JSON.stringify(load().comments);
    return getCrypto().then(function (cr) {
      var raw = loadRaw();
      if (!raw.shared) {
        // first sync in this browser: share comments written before the shared store existed
        raw.comments.forEach(function (c) { if (!c._sync) c._sync = 'put'; });
        raw.shared = true; saveRaw(raw);
      }
      var pending = raw.comments.filter(function (c) { return c._sync; });
      var sent = {};
      var req;
      if (pending.length) {
        // every pending change goes up in one request (one Blob upload)
        req = Promise.all(pending.map(function (c) {
          sent[c.id] = { sync: c._sync, updatedAt: c.updatedAt, resolvedAt: c.resolvedAt };
          return c._sync === 'del' ? null : encryptComment(cr, c).then(function (data) { return { id: c.id, data: data }; });
        })).then(function (recs) {
          return api(cr, 'POST', {
            put: recs.filter(Boolean),
            del: pending.filter(function (c) { return c._sync === 'del'; }).map(function (c) { return c.id; })
          });
        }).then(function (res) { clearFlags(sent); return res; });
      } else {
        req = api(cr, 'GET');
      }
      return req.then(function (res) {
        if (!res) return null; // 304: nothing new on the server
        // the Blob CDN may serve the previous version for up to a minute after a save
        var ver = typeof res.updatedAt === 'string' ? res.updatedAt : '';
        var seen = serverVersion || loadRaw().serverVersion || '';
        if (ver && seen && ver < seen) return null;
        if (ver) serverVersion = ver;
        var recs = Array.isArray(res.comments) ? res.comments : [];
        return Promise.all(recs.map(function (r) {
          return decryptComment(cr, r.data).then(function (c) { c = cleanComment(c); return c && c.id === r.id ? c : null; }, function () { return null; });
        }));
      }).then(function (server) {
        setSyncState('ok');
        if (!server) return;
        var data = loadRaw();
        var local = {};
        data.comments.forEach(function (c) { if (c._sync) local[c.id] = c; });
        var out = [];
        server.forEach(function (c) { if (c && !local[c.id]) out.push(c); });
        Object.keys(local).forEach(function (id) { out.push(local[id]); });
        data.comments = out; data.shared = true;
        if (serverVersion) data.serverVersion = serverVersion;
        saveRaw(data);
      });
    }).catch(function () {
      setSyncState('error');
      if (Date.now() - lastSyncErr > 120000) { lastSyncErr = Date.now(); status('אין חיבור לשרת ההערות. ההערות נשמרו בדפדפן ויישלחו כשהחיבור יחזור'); }
    }).then(function () {
      syncing = false;
      if (JSON.stringify(load().comments) !== before) renderAll();
      if (syncAgain) { syncAgain = false; queueSync(); }
    });
  }
  function queueSync() {
    if (!API) return;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(sync, 250);
  }
  function startSync() {
    if (!API) return;
    sync();
    setInterval(function () { if (document.visibilityState !== 'hidden') sync(); }, SYNC_EVERY);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') sync(); });
    window.addEventListener('online', function () { sync(); });
  }

  /* ---------------- counters ---------------- */
  function countLabel(list) {
    var n = list.length, open = openCount(list);
    if (!n) return '';
    var base = n === 1 ? 'הערה אחת' : n + ' הערות';
    return open === n ? base : base + ' · ' + (open ? open + ' פתוחות' : 'כולן טופלו');
  }
  function updateCounts() {
    var data = load();
    var all = data.comments, openAll = openCount(all);
    var syncNote = syncState === 'error' ? ' · לא מסונכרן' : '';
    var nav = $('#navCount');
    if (nav) {
      if (isDocPage) {
        var mine = all.filter(function (c) { return c.doc === DOC; });
        nav.textContent = 'פתוחות במסמך: ' + openCount(mine) + ' מתוך ' + mine.length + ' (סה"כ ' + openAll + ' פתוחות)' + syncNote;
      } else {
        nav.textContent = 'הערות פתוחות: ' + openAll + ' מתוך ' + all.length + syncNote;
      }
      nav.title = syncState === 'ok' ? 'ההערות משותפות לכל הקוראים' : syncState === 'error' ? 'אין חיבור לשרת ההערות כרגע' : '';
    }
    $all('[data-doc-count]').forEach(function (el) {
      el.textContent = countLabel(docComments(el.getAttribute('data-doc-count')));
    });
    var total = $('#totalCount');
    if (total) total.textContent = all.length ? 'סה"כ ' + all.length + ' הערות בכל המסמכים, ' + (openAll ? openAll + ' פתוחות.' : 'כולן טופלו.') : 'עדיין אין הערות.';
  }

  /* ---------------- export / import ---------------- */
  function exportComments() {
    var data = { version: 1, comments: load().comments.map(publicFields) };
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
      var data = loadRaw();
      var byIdMap = {};
      data.comments.forEach(function (c) { byIdMap[c.id] = c; });
      var added = 0, updated = 0, rejected = 0;
      incoming.comments.slice(0, MAX_COMMENTS).forEach(function (raw) {
        var c = cleanComment(raw);
        if (!c) { rejected++; return; }
        var cur = byIdMap[c.id];
        c._sync = 'put';
        if (!cur) { if (data.comments.length >= MAX_COMMENTS) { rejected++; return; } data.comments.push(c); byIdMap[c.id] = c; added++; return; }
        if (cur._sync === 'del') return;
        var a = cur.resolvedAt > (cur.updatedAt || '') ? cur.resolvedAt : (cur.updatedAt || cur.createdAt || '');
        var b = c.resolvedAt > (c.updatedAt || '') ? c.resolvedAt : (c.updatedAt || c.createdAt || '');
        if (b > a) { Object.keys(cur).forEach(function (k) { delete cur[k]; }); Object.assign(cur, c); updated++; }
      });
      saveRaw(data);
      renderAll(); queueSync();
      status('הייבוא הושלם: ' + added + ' הערות חדשות, ' + updated + ' עודכנו' + (rejected ? ', ' + rejected + ' נדחו' : '') + ', ' + load().comments.length + ' סה"כ');
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
        var item = document.createElement('div'); item.className = 'cmt' + (c.resolved ? ' resolved' : ''); item.setAttribute('data-id', c.id);
        var meta = document.createElement('div'); meta.className = 'meta';
        var au = document.createElement('span'); au.className = 'author'; au.textContent = c.author || '';
        var dt = document.createElement('span'); dt.textContent = fmtDate(c.createdAt);
        meta.appendChild(au); meta.appendChild(dt);
        if (c.updatedAt) { var up = document.createElement('span'); up.textContent = '(נערך ' + fmtDate(c.updatedAt) + ')'; meta.appendChild(up); }
        if (c.blockHash && c.blockHash !== curHash) { var ch = document.createElement('span'); ch.className = 'changed'; ch.textContent = 'הפסקה השתנתה'; meta.appendChild(ch); }
        if (c.resolved) {
          var rs = document.createElement('span'); rs.className = 'done';
          rs.textContent = 'טופלה' + (c.resolvedBy ? ' על ידי ' + c.resolvedBy : '') + (c.resolvedAt ? ' · ' + fmtDate(c.resolvedAt) : '');
          meta.appendChild(rs);
        }
        var text = document.createElement('div'); text.className = 'text'; text.textContent = c.text || '';
        var actions = document.createElement('div'); actions.className = 'actions';
        var rb = document.createElement('button'); rb.type = 'button';
        rb.className = c.resolved ? 'reopen' : 'resolve';
        rb.textContent = c.resolved ? 'פתיחה מחדש' : 'סימון כטופלה';
        rb.setAttribute('aria-label', (c.resolved ? 'פתיחה מחדש של ההערה של ' : 'סימון כטופלה של ההערה של ') + (c.author || ''));
        rb.addEventListener('click', function () { toggleResolved(blk, c.id); });
        var eb = document.createElement('button'); eb.type = 'button'; eb.textContent = 'עריכה';
        eb.setAttribute('aria-label', 'עריכת ההערה של ' + (c.author || ''));
        eb.addEventListener('click', function () { openForm(blk, c); });
        var db = document.createElement('button'); db.type = 'button'; db.textContent = 'מחיקה'; db.className = 'danger';
        db.setAttribute('aria-label', 'מחיקת ההערה של ' + (c.author || ''));
        db.addEventListener('click', function () {
          if (!confirm('למחוק את ההערה של ' + (c.author || '') + '?')) return;
          removeComment(c.id); renderBlock(blk); updateCounts(); status('ההערה נמחקה');
          blk.focus();
        });
        actions.appendChild(rb); actions.appendChild(eb); actions.appendChild(db);
        item.appendChild(meta); item.appendChild(text); item.appendChild(actions);
        if (form) host.insertBefore(item, form); else host.appendChild(item);
      });
      var open = openCount(list), badge = open ? String(open) : '✓';
      blk.classList.add('has-comments');
      blk.classList.toggle('all-resolved', !open);
      blk.setAttribute('data-count', badge);
      if (blk.tagName === 'TR' && blk.firstElementChild) blk.firstElementChild.setAttribute('data-count', badge);
    } else {
      blk.classList.remove('has-comments', 'all-resolved'); blk.removeAttribute('data-count');
      if (blk.tagName === 'TR' && blk.firstElementChild) blk.firstElementChild.removeAttribute('data-count');
      removeHostIfEmpty(blk);
    }
  }
  function toggleResolved(blk, id) {
    var c = getComment(id);
    if (!c) return;
    c = JSON.parse(JSON.stringify(c));
    if (c.resolved) {
      c.resolved = false; c.resolvedBy = null; c.resolvedAt = isoLocal(new Date());
    } else {
      var who = '';
      try { who = localStorage.getItem(AUTHOR_KEY) || ''; } catch (e) {}
      if (!who) {
        who = (window.prompt('השם שלך (יוצג ליד הסימון "טופלה"):', '') || '').trim().slice(0, 100);
        if (!who) return;
        try { localStorage.setItem(AUTHOR_KEY, who); } catch (e) {}
      }
      c.resolved = true; c.resolvedBy = who; c.resolvedAt = isoLocal(new Date());
    }
    upsertComment(c);
    renderBlock(blk); updateCounts();
    status(c.resolved ? 'ההערה סומנה כטופלה' : 'ההערה נפתחה מחדש');
    var btn = $('.cmt[data-id="' + id + '"] .actions button', hostFor(blk, false) || document);
    if (btn) btn.focus();
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
    var savedName = ''; try { savedName = localStorage.getItem(AUTHOR_KEY) || ''; } catch (e) {}
    nameIn.value = existing ? (existing.author || '') : savedName;
    textIn.value = existing ? (existing.text || '') : '';
    $('.cancel', form).addEventListener('click', function () { form.remove(); removeHostIfEmpty(blk); blk.focus(); });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var author = nameIn.value.trim(), text = textIn.value.trim();
      if (!author) { err.textContent = 'יש להזין שם'; nameIn.focus(); return; }
      if (!text) { err.textContent = 'יש להזין טקסט להערה'; textIn.focus(); return; }
      try { localStorage.setItem(AUTHOR_KEY, author); } catch (e2) {}
      if (existing) {
        var c = getComment(existing.id);
        if (c) { c = JSON.parse(JSON.stringify(c)); c.author = author; c.text = text; c.updatedAt = isoLocal(new Date()); upsertComment(c); }
      } else {
        upsertComment({
          id: uid(), doc: DOC, block: blk.getAttribute('data-block'), author: author, text: text,
          createdAt: isoLocal(new Date()), updatedAt: null, blockHash: hashText(blockText(blk)),
          resolved: false, resolvedBy: null, resolvedAt: null
        });
      }
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
        var d = document.createElement('div'); d.className = 'cmt' + (c.resolved ? ' resolved' : '');
        d.innerHTML = '<div class="meta"><span class="author"></span><span></span><span class="changed">הפסקה השתנתה</span></div><div class="text"></div>';
        $('.author', d).textContent = c.author || ''; $('.meta span:nth-child(2)', d).textContent = fmtDate(c.createdAt) + ' · ' + c.block;
        $('.text', d).textContent = c.text || '';
        if (c.resolved) { var rs = document.createElement('span'); rs.className = 'done'; rs.textContent = 'טופלה'; $('.meta', d).appendChild(rs); }
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
    var label = '';
    if (blk) {
      label = blk.tagName === 'TR'
        ? Array.prototype.map.call(blk.children, function (c) { return c.textContent.replace(/\s+/g, ' ').trim(); }).filter(Boolean).join(' | ')
        : blockText(blk);
      label = label.slice(0, 70);
    }
    s.push({ fromDoc: DOC, fromTitle: TITLE, fromAnchor: blk ? blk.getAttribute('data-block') : null, fromLabel: label, scrollY: window.scrollY, toDoc: toDoc });
    if (s.length > 20) s.shift();
    setRetStack(s);
  });
  function goBackTo(index) {
    var st = retStack(); var t = st[index];
    if (!t || DOC_IDS.indexOf(t.fromDoc) === -1) return;
    setRetStack(st.slice(0, index));
    sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ doc: t.fromDoc, anchor: t.fromAnchor, scrollY: t.scrollY }));
    var anchor = t.fromAnchor && ID_RE.test(t.fromAnchor) ? '#' + encodeURIComponent(t.fromAnchor) : '';
    location.href = t.fromDoc + '.html' + anchor;
  }
  function buildTrail(s) {
    var ul = $('#returnTrail'); if (!ul) return;
    ul.innerHTML = '';
    for (var i = s.length - 1; i >= 0; i--) {
      var e = s[i];
      if (DOC_IDS.indexOf(e.fromDoc) === -1) continue;
      var li = document.createElement('li');
      var b = document.createElement('button'); b.type = 'button';
      var steps = s.length - i;
      var num = document.createElement('span'); num.className = 'step'; num.textContent = steps === 1 ? 'צעד אחד אחורה' : steps + ' צעדים אחורה';
      var t = document.createElement('span'); t.className = 'where';
      t.textContent = (e.fromTitle || docTitles[e.fromDoc] || e.fromDoc) + (e.fromLabel ? ': ' + e.fromLabel + (e.fromLabel.length >= 70 ? '…' : '') : '');
      b.appendChild(num); b.appendChild(t);
      b.addEventListener('click', (function (idx) { return function () { goBackTo(idx); }; })(i));
      li.appendChild(b); ul.appendChild(li);
    }
  }
  function toggleTrail(open) {
    var ul = $('#returnTrail'), more = $('#returnDocMore');
    if (!ul || !more) return;
    var show = typeof open === 'boolean' ? open : ul.hidden;
    ul.hidden = !show; more.setAttribute('aria-expanded', show ? 'true' : 'false');
    more.textContent = show ? '▼' : '▲';
    if (show) { var first = ul.querySelector('button'); if (first) first.focus(); } else { more.focus(); }
  }
  function showReturnDoc() {
    var box = $('#returnDoc'); if (!box) return;
    var s = retStack(); var top = s[s.length - 1];
    if (!top || top.toDoc !== DOC || DOC_IDS.indexOf(top.fromDoc) === -1) { box.hidden = true; return; }
    $('#returnDocBtn').textContent = 'חזרה ל-' + (top.fromTitle || docTitles[top.fromDoc] || top.fromDoc);
    box.hidden = false;
    var more = $('#returnDocMore');
    if (more) { more.hidden = s.length < 2; more.onclick = function () { toggleTrail(); }; }
    buildTrail(s);
    $('#returnDocBtn').onclick = function () { goBackTo(retStack().length - 1); };
    $('#returnDocClose').onclick = function () { var st = retStack(); st.pop(); setRetStack(st); box.hidden = true; toggleTrail(false); };
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { var ul = $('#returnTrail'); if (ul && !ul.hidden) toggleTrail(false); }
  });
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
    if (!a || a.closest('.topbar, .outline') || a.getAttribute('href') === '#main') return;
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

  /* ---------------- outline drawer and reading progress ---------------- */
  var outline = $('#outline'), outlineBtn = $('#outlineBtn'), outlineList = $('#outlineList'), scrim = $('#outlineScrim');
  var heads = [], activeIdx = -1, outlineTimer = null, spyQueued = false;
  function buildOutline() {
    var art = $('article.document');
    if (!art || !outline || !outlineBtn || !outlineList) return;
    var all = $all('h2.blk, h3.blk', art).filter(function (h) { return !h.classList.contains('pagemark'); });
    var topTag = all.filter(function (h) { return h.tagName === 'H2'; }).length >= 2 ? 'H2' : 'H3';
    var items = topTag === 'H3' ? all.filter(function (h) { return h.tagName === 'H3'; }) : all;
    if (items.length < 3) return;
    var curLi = null, curSub = null;
    items.forEach(function (h) {
      var a = document.createElement('a');
      a.href = '#' + encodeURIComponent(h.id);
      a.textContent = h.textContent.replace(/\s+/g, ' ').trim();
      a.addEventListener('click', function (e) {
        e.preventDefault();
        closeOutline(false);
        history.replaceState(null, '', '#' + encodeURIComponent(h.id));
        h.scrollIntoView({ block: 'start' });
        flash(h);
        h.focus({ preventScroll: true });
      });
      var li = document.createElement('li'); li.appendChild(a);
      if (h.tagName === topTag || !curLi) {
        li.className = 'o-top' + (h.classList.contains('part') ? ' o-part' : '');
        outlineList.appendChild(li); curLi = li; curSub = null;
      } else {
        if (!curSub) { curSub = document.createElement('ol'); curLi.appendChild(curSub); curLi.classList.add('has-sub'); }
        curSub.appendChild(li);
      }
      heads.push({ h: h, a: a, group: curLi });
    });
    outlineBtn.hidden = false;
    outlineBtn.addEventListener('click', function () { if (outline.hidden) openOutline(); else closeOutline(true); });
    $('#outlineClose').addEventListener('click', function () { closeOutline(true); });
    if (scrim) scrim.addEventListener('click', function () { closeOutline(true); });
  }
  function openOutline() {
    clearTimeout(outlineTimer);
    outline.hidden = false; if (scrim) scrim.hidden = false;
    requestAnimationFrame(function () { outline.classList.add('open'); if (scrim) scrim.classList.add('open'); });
    outlineBtn.setAttribute('aria-expanded', 'true');
    var cur = $('a[aria-current]', outline) || $('a', outline);
    if (cur) {
      outlineList.scrollTop += cur.getBoundingClientRect().top - outlineList.getBoundingClientRect().top - outlineList.clientHeight / 3;
      cur.focus({ preventScroll: true });
    }
  }
  function closeOutline(refocus) {
    if (!outline || outline.hidden) return;
    outline.classList.remove('open'); if (scrim) scrim.classList.remove('open');
    outlineBtn.setAttribute('aria-expanded', 'false');
    clearTimeout(outlineTimer);
    outlineTimer = setTimeout(function () { outline.hidden = true; if (scrim) scrim.hidden = true; }, 220);
    if (refocus) outlineBtn.focus();
  }
  function spy() {
    spyQueued = false;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var idx = -1;
    for (var i = 0; i < heads.length; i++) {
      if (heads[i].h.getBoundingClientRect().top <= 140) idx = i; else break;
    }
    var bar = $('#progressBar');
    if (bar) bar.style.transform = 'scaleX(' + (max > 0 ? Math.min(1, window.scrollY / max) : 0) + ')';
    if (idx === activeIdx) return;
    var prev = heads[activeIdx];
    if (prev) { prev.a.removeAttribute('aria-current'); prev.group.classList.remove('open'); }
    activeIdx = idx;
    var cur = heads[idx];
    if (cur) { cur.a.setAttribute('aria-current', 'location'); cur.group.classList.add('open'); }
  }
  function queueSpy() { if (!spyQueued) { spyQueued = true; requestAnimationFrame(spy); } }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && outline && !outline.hidden) closeOutline(true);
  });

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
  // on narrow screens the document tabs scroll sideways: bring the current one into view
  var curTab = $('.topbar .doclinks a[aria-current]');
  if (curTab) {
    var tabs = curTab.closest('.doclinks'), tr = tabs.getBoundingClientRect(), cr = curTab.getBoundingClientRect();
    if (tabs.scrollWidth > tabs.clientWidth) tabs.scrollLeft += cr.left - tr.left - (tr.width - cr.width) / 2;
  }
  renderAll();
  startSync();
  if (isDocPage) {
    buildOutline();
    showReturnDoc();
    if (!restoreIfNeeded()) jumpToHash();
    window.addEventListener('scroll', queueSpy, { passive: true });
    window.addEventListener('resize', queueSpy);
    queueSpy();
  }
  window.addEventListener('storage', function (e) { if (e.key === STORE_KEY) renderAll(); });
})();
