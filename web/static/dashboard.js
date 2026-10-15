/* Virtual Printer dashboard: theme switcher, copy buttons, live refresh.
   (The tiny "apply saved theme" snippet that runs before first paint is inline in base.html.) */
(function () {
  var root = document.documentElement;
  var THEME_KEY = 'vp-theme';      // localStorage key; value is 'light' or 'dark' (absent = follow system)
  var DONE = ['text-emerald-500', 'border-emerald-500'];  // classes shown on a copy button right after copying
  var REFRESH_MS = 3000;           // how often the live area is re-rendered

  /* ---- theme switcher ---- */
  function currentTheme() {
    var t = root.getAttribute('data-theme');
    return t === 'light' || t === 'dark' ? t : 'system';
  }
  function markActiveTheme() {
    var c = currentTheme();
    document.querySelectorAll('[data-theme-set]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.themeSet === c));
    });
  }
  function setTheme(t) {
    if (t === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', t);
    try {
      if (t === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, t);
    } catch (_) {}
    markActiveTheme();
  }

  /* ---- copy to clipboard (Clipboard API, falling back to execCommand) ---- */
  function legacyCopy(text, done) {
    var a = document.createElement('textarea');
    a.value = text;
    a.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(a);
    a.select();
    try { if (document.execCommand('copy')) done(); } catch (_) {}
    a.remove();
  }
  function copyText(text, done) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text, done); });
    } else {
      legacyCopy(text, done);
    }
  }

  /* One delegated click handler, so it keeps working after each live refresh swaps the DOM. */
  document.addEventListener('click', function (e) {
    var tb = e.target.closest('[data-theme-set]');
    if (tb) { setTheme(tb.dataset.themeSet); return; }

    var b = e.target.closest('[data-copy]');
    if (!b) return;
    copyText(b.dataset.copy, function () {
      var s = b.querySelector('span');
      s.textContent = 'Copied';
      DONE.forEach(function (c) { b.classList.add(c); });
      setTimeout(function () { s.textContent = 'Copy'; DONE.forEach(function (c) { b.classList.remove(c); }); }, 1200);
    });
  });

  /* ---- live refresh: fetch the #live partial and swap it in ---- */
  var foot = document.getElementById('foot');
  var upd = document.getElementById('upd');
  setInterval(async function () {
    try {
      var r = await fetch('/?partial=1', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      document.getElementById('live').innerHTML = await r.text();
      markActiveTheme();  // the switcher is inside #live, so its highlighted button must be restored
      foot.classList.remove('err');
      upd.textContent = 'Live \u00b7 updated ' + new Date().toLocaleTimeString();
    } catch (e) {
      foot.classList.add('err');
      // "HTTP 500" = the server answered but a template failed (see the server log); otherwise it's unreachable
      upd.textContent = (e.message || '').indexOf('HTTP') === 0
        ? 'Server error (' + e.message + ') \u00b7 check the server log'
        : 'Connection lost \u00b7 retrying\u2026';
    }
  }, REFRESH_MS);

  markActiveTheme();
})();
