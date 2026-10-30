import sys, time, json, base64, subprocess, atexit, os
sys.path.insert(0, '/vol2/@apphome/hermes-agent/data/workspace/hermes-si')
from verify import CDP
CDP_PORT = 18766
CHR = '/vol2/@apphome/hermes-agent/data/cache/scratch/chr-strata-ui'
OUT = '/vol2/@apphome/hermes-agent/data/workspace/strata-fpk'
VM = 'http://192.168.30.30:5666'
sh = lambda a: subprocess.run(a, capture_output=True, text=True)
chrome = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--no-sandbox', '--disable-gpu',
                          '--ignore-certificate-errors', '--remote-allow-origins=*', '--window-size=1920,1080',
                          f'--user-data-dir={CHR}', f'--remote-debugging-port={CDP_PORT}', 'about:blank'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(lambda: chrome.terminate())
for _ in range(60):
    if sh(['curl', '-s', '--max-time', '2', f'http://127.0.0.1:{CDP_PORT}/json/version']).stdout.strip():
        break
    time.sleep(1)
c = CDP(CDP_PORT); c.start(CDP_PORT)
c.cmd('Page.enable'); c.cmd('Runtime.enable'); c.cmd('Network.enable')
c.cmd('Emulation.setDeviceMetricsOverride', width=1920, height=1080, deviceScaleFactor=1, mobile=False)

def js(expr):
    r = c.cmd('Runtime.evaluate', expression=expr, returnByValue=True)
    return r.get('result', {}).get('value')

def shot(name):
    r = c.cmd('Page.captureScreenshot', format='png')
    open(f'{OUT}/{name}', 'wb').write(base64.b64decode(r['data']))

def dump(sel='div,span,button,a,input', limit=800):
    expr = ("JSON.stringify(Array.from(document.querySelectorAll(" + json.dumps(sel)
            + ")).slice(0," + str(limit) + ").map(e=>{const r=e.getBoundingClientRect();return [(e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,26),Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]}))")
    return json.loads(js(expr) or '[]')

def click_at(x, y):
    for ty in ('mousePressed', 'mouseReleased'):
        c.cmd('Input.dispatchMouseEvent', type=ty, x=x, y=y, button='left', clickCount=1,
              buttons=1 if ty == 'mousePressed' else 0)

def click_text(target, tries=5):
    for _ in range(tries):
        for t, x, y, w, h in dump('div,span,button,a', 2000):
            if t == target:
                click_at(x + w // 2, y + h // 2)
                return True
        time.sleep(2)
    return False

def login():
    c.cmd('Page.navigate', url=VM + '/')
    time.sleep(10)
    if js("localStorage.getItem('fnos-Secret')"):
        print('already logged in', flush=True); return True
    for _ in range(15):
        inp = [it for it in dump('input', 10) if it[3] > 100]
        if len(inp) >= 2:
            break
        time.sleep(2)
    if len(inp) < 2:
        print('no login inputs; body:', (js('document.body.innerText') or '')[:150], flush=True)
        return False
    for cx, cy, txt in ((inp[0][1] + inp[0][3]//2, inp[0][2] + inp[0][4]//2, 'FNOS'),
                        (inp[1][1] + inp[1][3]//2, inp[1][2] + inp[1][4]//2, '123456')):
        click_at(cx, cy); time.sleep(0.3)
        c.cmd('Input.insertText', text=txt); time.sleep(0.4)
    click_text('登录')
    time.sleep(12)
    return bool(js("localStorage.getItem('fnos-Secret')"))

assert login(), 'login failed'

# open strata app via gateway prefix path
c.cmd('Page.navigate', url=VM + '/app/strata-app/')
net = []
fails = []
deadline = time.time() + 25
while time.time() < deadline:
    try:
        m = c.ev.get(timeout=1)
    except Exception:
        continue
    me = m.get('method')
    if me == 'Network.responseReceived':
        r = m['params']['response']
        net.append((r['status'], r['mimeType'], r['url']))
    elif me == 'Network.loadingFailed':
        fails.append((m['params'].get('errorText'), m['params'].get('type')))

print('--- responses:', len(net))
for st, mt, u in net:
    if st >= 400 or mt in ('text/css',) or 'index-' in u or 'folder' in u:
        print(st, mt, u[:110], flush=True)
print('--- load failures:', fails[:10], flush=True)

# check CSS actually applied
cssok = js("""(()=>{const l=[...document.querySelectorAll('link[rel=stylesheet]')].map(x=>x.href);
const el=document.body; const bg=getComputedStyle(el).backgroundColor;
const root=document.getElementById('root'); const kids=root?root.children.length:0;
return JSON.stringify({sheets:l, bodyBg:bg, rootKids:kids, styleTags:[...document.querySelectorAll('style')].length})})()""")
print('--- css state:', cssok, flush=True)
shot('strata_ui_vm.png')
txt = (js('document.body.innerText') or '').replace('\n', ' | ')[:300]
print('--- body text:', txt, flush=True)
