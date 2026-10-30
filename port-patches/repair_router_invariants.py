#!/usr/bin/env python3
# Invariant repair for index.mjs router hunk lost to upstream drift (0.2.8).
# Idempotent: skips anything already present. Prints counts for audit.
import sys

p = sys.argv[1]
s = open(p).read()
report = []

routes = """    if (path === '/api/folder-request/pending' && method === 'GET') return json(res, 200, { requests: [...FOLDER_REQUESTS.values()].map(e => e.meta) });
  if (path === '/api/folder-request/resolve' && method === 'POST') {
    const body = await readBody(req);
    const entry = FOLDER_REQUESTS.get(String(body?.id ?? ''));
    if (!entry) return json(res, 200, { expired: true });
    clearTimeout(entry.timer);
    FOLDER_REQUESTS.delete(String(body?.id ?? ''));
    if (body?.cancel) { entry.resolve({ cancelled: true }); return json(res, 200, { ok: true }); }
    const typed = await typedFolder(body?.path, { isAbsolute });
    if (!typed) return error(res, 400, 'Choose an existing folder on the server or type an absolute path.');
    entry.resolve({ path: typed });
    return json(res, 200, { ok: true });
  }
  if (path === '/api/setup' && method === 'GET') return json(res, 200, setupState());"""
anchor = "  if (path === '/api/setup' && method === 'GET') return json(res, 200, setupState());"
if "path === '/api/folder-request/pending'" not in s:
    assert s.count(anchor) == 1, ('setup-anchor-count', s.count(anchor))
    s = s.replace(anchor, routes, 1)
    report.append('routes-injected=1')
else:
    report.append('routes-present-skip')

seal = """  if (path === '/api/updates/check' && method === 'GET') {
    // fnOS port: updates are re-ported manually as new fpk releases (this build's
    // version is STRATA_APP_VERSION); never fetch the upstream GitHub feed, and
    // never offer in-app install/update buttons.
    const av = process.env.STRATA_APP_VERSION || setupState().app.version || null;
    let ev = native.state().version ?? null;
    try { ev = ev ?? (await engine.config()).engine.version ?? null; } catch { /* engine offline */ }
    return json(res, 200, { checkedAt: new Date().toISOString(), source: 'fnos-port-managed', strata: { installedVersion: av, status: 'current', message: 'This build is maintained as an fnOS package; updates arrive as new fpk releases.', canInstall: false }, engine: { installedVersion: ev, status: 'current', canInstall: false } });
  }"""
ol = "  if (path === '/api/updates/check' && method === 'GET' && control?.updateCheck) return json(res, 200, await checkComponentUpdates({ control, appUpdater, appVersion: process.env.STRATA_APP_VERSION || null }));"
if 'fnos-port-managed' not in s:
    assert s.count(ol) == 1, ('updates-anchor-count', s.count(ol))
    s = s.replace(ol, seal, 1)
    report.append('seal-injected=1')
else:
    report.append('seal-present-skip')

n = s.count('await chooseFolder(')
if n:
    s = s.replace('await chooseFolder(', 'await pickFolderBrowser(')
open(p, 'w').write(s)
report.append(f'chooseFolder-fixed={n}')
print(' '.join(report))
