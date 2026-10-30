/* fnOS-port: in-page folder picker for headless servers.
 * When the Strata host needs a folder (model dir, skills, workspace), the API
 * call blocks and registers a request here; this overlay browses server
 * folders via /api/local-models/browse and posts the choice back. */
(function () {
  var polling = false, active = null, modal = null, cur = '';

  // Anchor API paths to this file's own URL (…/folder-picker.js -> its dir),
  // so they resolve correctly behind the fnOS gateway prefix regardless of
  // SPA route changes and independent of the proxy's JS path rewriting.
  var SELF = (document.currentScript && document.currentScript.src) || document.baseURI;
  function url(p) { var b = SELF; var i = b.lastIndexOf('/'); if (i >= 0) b = b.slice(0, i + 1); return new URL(p, b).href; }
  function api(p) { return fetch(url(p), { credentials: 'same-origin' }).then(function (r) { return r.json(); }); }

  function css() {
    var s = document.createElement('style');
    s.textContent = '.fp-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99998;display:flex;align-items:center;justify-content:center}'
      + '.fp-box{background:var(--surface,#fff);color:var(--ink,#111);border:1px solid rgba(128,128,128,.35);border-radius:12px;max-width:560px;width:92%;max-height:78vh;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.35);font:14px/1.45 system-ui,sans-serif}'
      + '.fp-head{padding:14px 16px;font-weight:600;border-bottom:1px solid rgba(128,128,128,.25)}'
      + '.fp-path{padding:8px 16px;font:12px ui-monospace,monospace;opacity:.75;word-break:break-all;display:flex;gap:8px;align-items:center}'
      + '.fp-path button{flex:none;background:none;border:1px solid rgba(128,128,128,.4);color:inherit;border-radius:6px;padding:2px 8px;cursor:pointer}'
      + '.fp-list{overflow:auto;flex:1;padding:4px 8px;min-height:120px}'
      + '.fp-row{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:8px;cursor:pointer}'
      + '.fp-row:hover{background:rgba(128,128,128,.14)}'
      + '.fp-file{opacity:.45;cursor:default}'
      + '.fp-note{padding:8px 16px;font-size:12px;opacity:.7}'
      + '.fp-foot{display:flex;gap:8px;justify-content:flex-end;padding:12px 16px;border-top:1px solid rgba(128,128,128,.25)}'
      + '.fp-foot button{border-radius:8px;padding:7px 14px;cursor:pointer;border:1px solid rgba(128,128,128,.4);background:transparent;color:inherit}'
      + '.fp-primary{background:#2f6fed!important;color:#fff!important;border-color:#2f6fed!important}'
      + '.fp-typed{width:100%;box-sizing:border-box;border:1px solid rgba(128,128,128,.4);border-radius:8px;background:transparent;color:inherit;padding:7px 10px;font:13px ui-monospace,monospace}';
    document.head.appendChild(s);
  }

  function load(dir) {
    api("api/local-models/browse?directory=" + encodeURIComponent(dir)).then(function (d) {
      if (!modal) return;
      if (d.error) { note(d.error); return; }
      cur = d.directory;
      pathEl.textContent = cur;
      upBtn.style.display = d.parent ? '' : 'none';
      upBtn.onclick = function () { load(d.parent); };
      list.innerHTML = '';
      (d.entries || []).forEach(function (e) {
        var row = document.createElement('div');
        row.className = 'fp-row' + (e.kind === 'folder' ? '' : ' fp-file');
        row.textContent = (e.kind === 'folder' ? '\u{1F4C1} ' : '\u{1F4C4} ') + e.name;
        if (e.kind === 'folder') row.onclick = function () { load(e.path); };
        list.appendChild(row);
      });
    }).catch(function () { if (modal) note('加载失败，可直接输入路径。'); });
  }

  var pathEl, upBtn, list, noteEl, typeEl;
  function note(t) { if (noteEl) noteEl.textContent = t; }

  function open(meta) {
    active = meta;
    css();
    modal = document.createElement('div');
    modal.className = 'fp-mask';
    modal.innerHTML = '<div class="fp-box"><div class="fp-head"></div>'
      + '<div class="fp-path"><span></span><button type="button">上级</button></div>'
      + '<div class="fp-list"></div><div class="fp-note"></div>'
      + '<div class="fp-path" style="padding-top:0"><input class="fp-typed" placeholder="或直接输入服务器上的绝对路径"></div>'
      + '<div class="fp-foot"><button type="button" class="fp-cancel">取消</button><button type="button" class="fp-ok fp-primary">选择此文件夹</button></div></div>';
    document.body.appendChild(modal);
    modal.querySelector('.fp-head').textContent = meta.prompt || '选择文件夹';
    pathEl = modal.querySelector('.fp-path span');
    upBtn = modal.querySelector('.fp-path button');
    list = modal.querySelector('.fp-list');
    noteEl = modal.querySelector('.fp-note');
    typeEl = modal.querySelector('.fp-typed');
    modal.querySelector('.fp-cancel').onclick = function () { resolve({ cancel: true }); };
    modal.querySelector('.fp-ok').onclick = function () { resolve({ path: (typeEl.value || '').trim() || cur }); };
    typeEl.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); resolve({ path: typeEl.value.trim() || cur }); } });
    load(meta.startPath || '');
  }

  function resolve(body) {
    var id = active && active.id;
    close();
    if (id) { body.id = id; fetch(url("api/folder-request/resolve"), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), credentials: 'same-origin' }).catch(function () {}); }
  }

  function close() { if (modal) { modal.remove(); modal = null; } active = null; }

  function tick() {
    if (polling) return;
    polling = true;
    api("api/folder-request/pending").then(function (d) {
      polling = false;
      if (!modal && d.requests && d.requests.length) open(d.requests[0]);
    }).catch(function () { polling = false; });
  }
  setInterval(tick, 1200);
})();
