#!/usr/bin/env python3
# Local L2 browser E2E for Strata FNOS 0.2.8 sim: chromium CDP through sock_proxy unix socket
# (tcp2unix bridge), gateway prefix path. Assertions: hook injected, zero 4xx, SSE connected.
import json, os, socket, subprocess, sys, threading, time, urllib.request

import websocket  # websocket-client

W = '/vol2/@apphome/hermes-si/data/hermes-home/port/strata-work'
SOCK = os.path.join(W, 'sim028/target/strata.sock')
SCRATCH = '/vol2/@apphome/hermes-si/data/hermes-home/cache/scratch'
PROFILE = os.path.join(SCRATCH, 'strata-e2e-profile')
CDP_PORT = 9347
BRIDGE_PORT = 18099

def free_wait(url, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            urllib.request.urlopen(url, timeout=2); return True
        except Exception:
            time.sleep(0.5)
    return False

def bridge(stop=[False]):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', BRIDGE_PORT)); srv.listen(8)
    srv.settimeout(1)
    def pump(c):
        u = socket.socket(socket.AF_UNIX); u.connect(SOCK)
        def c2u():
            try:
                while True:
                    d = c.recv(65536)
                    if not d: break
                    u.sendall(d)
            except Exception: pass
            try: u.close()
            except Exception: pass
        def u2c():
            try:
                while True:
                    d = u.recv(65536)
                    if not d: break
                    c.sendall(d)
            except Exception: pass
            try: c.close()
            except Exception: pass
        threading.Thread(target=c2u, daemon=True).start()
        threading.Thread(target=u2c, daemon=True).start()
    while not stop[0]:
        try:
            c, _ = srv.accept(); threading.Thread(target=pump, args=(c,), daemon=True).start()
        except socket.timeout:
            continue
    srv.close()

brk = [False]
threading.Thread(target=bridge, args=(brk,), daemon=True).start()

proc = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--no-sandbox',
    f'--user-data-dir={PROFILE}', f'--remote-debugging-port={CDP_PORT}',
    '--remote-allow-origins=*', '--lang=zh-CN', '--window-size=1920,1080', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    assert free_wait(f'http://127.0.0.1:{CDP_PORT}/json/version', 20), 'chromium CDP not up'
    req = urllib.request.Request(f'http://127.0.0.1:{CDP_PORT}/json/new?url=about%3Ablank', method='PUT')
    tab = json.load(urllib.request.urlopen(req, timeout=5))
    ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=30, suppress_origin=True)
    mid = [0]
    def cmd(method, **params):
        mid[0] += 1
        ws.send(json.dumps({'id': mid[0], 'method': method, 'params': params}))
        while True:
            m = json.loads(ws.recv())
            if m.get('id') == mid[0]:
                if 'error' in m: raise RuntimeError(m['error'])
                return m.get('result', {})
    events = []
    ws.settimeout(1)
    cmd('Network.enable'); cmd('Page.enable')
    cmd('Emulation.setDeviceMetricsOverride', width=1600, height=1000, deviceScaleFactor=1, mobile=False)
    cmd('Page.navigate', url=f'http://127.0.0.1:{BRIDGE_PORT}/app/strata-app/')
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            m = json.loads(ws.recv())
        except Exception:
            continue
        if m.get('method') == 'Network.responseReceived':
            r = m['params']['response']
            events.append((r['status'], r['url'], r.get('mimeType', '')))
        elif m.get('method') == 'Network.loadingFailed':
            events.append(('FAIL', m['params'].get('errorText', ''), m['params'].get('type', '')))
    hook = cmd('Runtime.evaluate', expression='window.__STRATA_HOST__', returnByValue=True)
    sse = cmd('Runtime.evaluate', expression="(() => { try { const es = new EventSource('/app/strata-app/api/events'); window.__sse_test__ = es; return 'created'; } catch(e) { return String(e); } })()")
    time.sleep(3)
    # screenshot for the record
    shot = cmd('Page.captureScreenshot', format='png')
    outpng = os.path.join(W, 'e2e028_local.png')
    import base64
    open(outpng, 'wb').write(base64.b64decode(shot['data']))

    bad = [e for e in events if isinstance(e[0], int) and e[0] >= 400]
    failed = [e for e in events if e[0] == 'FAIL']
    prefix_hits = [e for e in events if '/app/strata-app/' in e[1]]
    sse_resp = [e for e in events if 'event-stream' in e[2]]
    fp = [e for e in events if 'folder-picker.js' in e[1]]
    print('TOTAL-RESPONSES', len(events))
    print('PREFIX-HITS', len(prefix_hits))
    print('4xx:', bad)
    print('LOADFAILS:', failed[:8])
    print('HOOK:', hook.get('result', {}).get('value'))
    print('SSE-RESPONSES(event-stream):', len(sse_resp), [e[0] for e in sse_resp])
    print('FOLDER-PICKER:', fp)
    print('SHOT:', outpng)
    ok = (not bad) and (not failed) and hook.get('result', {}).get('value') == '/app/strata-app' and len(sse_resp) >= 1 and len(fp) >= 1
    print('E2E-LOCAL:', 'PASS' if ok else 'FAIL')
finally:
    brk[0] = True
    proc.terminate()
