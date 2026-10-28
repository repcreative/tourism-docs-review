/* Password gate: pages are stored AES-256-CBC encrypted; the key is derived from the
   password with PBKDF2-SHA256 in the browser and cached for this tab only. */
(function () {
  'use strict';
  var KEY = 'tourism-docs-key';
  var P = document.getElementById('payload');
  if (!P) return;
  var cfg = {
    iv: P.getAttribute('data-iv'), salt: P.getAttribute('data-salt'),
    iter: parseInt(P.getAttribute('data-iter'), 10) || 600000,
    ct: P.textContent.replace(/\s+/g, '')
  };
  var form = document.getElementById('gateForm'), pwdIn = document.getElementById('gatePwd');
  var btn = document.getElementById('gateBtn'), msg = document.getElementById('gateMsg'), err = document.getElementById('gateErr');

  function hex2buf(h) { var a = new Uint8Array(h.length / 2); for (var i = 0; i < a.length; i++) a[i] = parseInt(h.substr(i * 2, 2), 16); return a; }
  function buf2hex(b) { return Array.prototype.map.call(new Uint8Array(b), function (x) { return ('0' + x.toString(16)).slice(-2); }).join(''); }
  function b642buf(s) { var bin = atob(s); var a = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a; }

  function derive(pwd) {
    return crypto.subtle.importKey('raw', new TextEncoder().encode(pwd), 'PBKDF2', false, ['deriveBits'])
      .then(function (km) { return crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: hex2buf(cfg.salt), iterations: cfg.iter }, km, 256); });
  }
  function decrypt(raw) {
    return crypto.subtle.importKey('raw', raw, { name: 'AES-CBC' }, false, ['decrypt'])
      .then(function (key) { return crypto.subtle.decrypt({ name: 'AES-CBC', iv: hex2buf(cfg.iv) }, key, b642buf(cfg.ct)); })
      .then(function (pt) {
        var txt = new TextDecoder().decode(pt);
        if (txt.slice(0, 4) !== 'OK::') throw new Error('bad password');
        return JSON.parse(txt.slice(4));
      });
  }
  function runScripts(container) {
    var scripts = Array.prototype.slice.call(container.querySelectorAll('script'));
    return scripts.reduce(function (p, old) {
      return p.then(function () {
        return new Promise(function (resolve) {
          var s = document.createElement('script');
          if (old.getAttribute('src')) { s.src = old.getAttribute('src'); s.onload = resolve; s.onerror = resolve; }
          else { s.textContent = old.textContent; }
          old.parentNode.replaceChild(s, old);
          if (!old.getAttribute('src')) resolve();
        });
      });
    }, Promise.resolve());
  }
  function render(data) {
    document.title = data.title;
    document.body.setAttribute('data-doc', data.doc);
    document.body.setAttribute('data-title', data.title);
    document.body.innerHTML = data.html;
    return runScripts(document.body);
  }
  function fail(text) {
    err.textContent = text; msg.textContent = '';
    btn.disabled = false; pwdIn.disabled = false; pwdIn.value = ''; pwdIn.focus();
  }

  if (!window.crypto || !crypto.subtle) {
    fail('הדפדפן הזה לא תומך בפענוח. יש לפתוח את האתר בכתובת https או בדפדפן עדכני.');
    return;
  }
  var cached = null;
  try { cached = sessionStorage.getItem(KEY); } catch (e) { cached = null; }
  if (cached) {
    form.hidden = true;
    decrypt(hex2buf(cached)).then(render).catch(function () {
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      form.hidden = false; pwdIn.focus();
    });
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var pwd = pwdIn.value;
    if (!pwd) { fail('יש להזין סיסמה'); return; }
    err.textContent = ''; msg.textContent = 'מפענח…'; btn.disabled = true; pwdIn.disabled = true;
    var raw;
    derive(pwd).then(function (bits) { raw = bits; return decrypt(bits); })
      .then(function (data) {
        try { sessionStorage.setItem(KEY, buf2hex(raw)); } catch (e2) {}
        return render(data);
      })
      .catch(function () { fail('סיסמה שגויה'); });
  });
})();
