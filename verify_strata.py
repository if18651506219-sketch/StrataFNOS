#!/usr/bin/env python3
"""Strata FNOS 成品 fpk 静态自检（L0）+ 版本一致性断言。
用法: python3 verify_strata.py [fpk路径] [期望官方版本]

检查项:
  L0.manifest            appname/display/version 与期望一致
  L0.manifest-checksum   manifest.checksum == MD5(app.tgz)（fnOS 安装校验字段）
  L0.root-icons          包根 ICON.PNG / ICON_256.PNG 存在
  L0.zero-inner-links    app.tgz 内无 symlink / hardlink 条目
  L0.cmd-syntax          cmd/* 全部 bash -n 通过
  L0.app-version         cmd/main STRATA_APP_VERSION == manifest version
  L0.no-hardcoded-volume 生命周期脚本不写死 /vol2
  L0.sock-proxy          代理存在且含 0.2.10 回归修复标记
  L0.port-patches        host 补丁标记全部在位
  INFO                   control VERSION（组件版本与 manifest 是两套编号）

0.2.10 修复：旧版只查 appname/display/version，不校验成品 checksum，也不查
包根图标 —— 正是 HermesFNOS 0.22.7.2 被应用中心拒装的同款坑。另外 marker
文案写死 "/7 markers"（实际 11 项），现已改为实际数量。
"""
import sys, os, tarfile, subprocess, tempfile, hashlib

FPK = sys.argv[1] if len(sys.argv) > 1 else 'strata-app.fpk'
WANT_VER = sys.argv[2] if len(sys.argv) > 2 else '0.2.3'

fails = []
def chk(name, ok, detail=''):
    print(f'{"PASS" if ok else "FAIL"} | {name} | {detail}')
    if not ok:
        fails.append(name)

tmp = tempfile.mkdtemp(prefix='strataverify_')
subprocess.run(['tar', '-xf', FPK, '-C', tmp], check=True)

def read(p):
    try:
        return open(os.path.join(tmp, p), encoding='utf-8', errors='replace').read()
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return ''

man = read('manifest')
def mf(key):
    for line in man.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            if k.strip() == key:
                return v.strip()
    return ''
chk('L0.manifest', mf('appname') == 'strata-app' and mf('display_name') == 'Strata FNOS' and mf('version') == WANT_VER,
    f"appname={mf('appname')} display={mf('display_name')} version={mf('version')} (want {WANT_VER})")

# fnOS 安装器用 manifest.checksum 校验 app.tgz；fnpack 生成成品时写入，
# 重封/手工改包后值会过期，缺字段直接拒装。
tg = os.path.join(tmp, 'app.tgz')
h = hashlib.md5()
with open(tg, 'rb') as f:
    for block in iter(lambda: f.read(1024 * 1024), b''):
        h.update(block)
actual_md5 = h.hexdigest()
want_md5 = mf('checksum')
chk('L0.manifest-checksum', want_md5 == actual_md5 and len(want_md5) == 32,
    f'manifest={want_md5 or "MISSING"} md5(app.tgz)={actual_md5}')

icons = [p for p in ('ICON.PNG', 'ICON_256.PNG') if os.path.isfile(os.path.join(tmp, p))]
chk('L0.root-icons', len(icons) == 2, f'found={icons} (want ICON.PNG + ICON_256.PNG)')

# app.tgz 内层：软链接与硬链接条目都会让安装器解包失败
links = 0
with tarfile.open(tg) as t:
    for m in t.getmembers():
        if m.issym() or m.islnk():
            links += 1
chk('L0.zero-inner-links', links == 0, f'link-entries={links}')

app = os.path.join(tmp, 'appx')
os.makedirs(app, exist_ok=True)
with tarfile.open(tg) as t:
    # Python 3.14 defaults to the 'data' filter, which REJECTS link entries —
    # exactly what L0.zero-inner-links is supposed to detect. Keep the tar
    # semantics (links allowed, absolute paths still rejected) and fall back
    # for older interpreters that have no `filter` parameter.
    try:
        t.extractall(app, filter='tar')
    except TypeError:
        t.extractall(app)

cmd_dir = os.path.join(tmp, 'cmd')
bad = [f for f in os.listdir(cmd_dir)
       if subprocess.run(['bash', '-n', os.path.join(cmd_dir, f)]).returncode != 0]
chk('L0.cmd-syntax', not bad, f'bad={bad}')
main_src = open(os.path.join(cmd_dir, 'main')).read()
chk('L0.app-version', f'STRATA_APP_VERSION="{WANT_VER}"' in main_src,
    'cmd/main STRATA_APP_VERSION matches official manifest version')

vol_bad = [p for p in ('cmd/main', 'cmd/install_callback') if '/vol2/' in read(p)]
chk('L0.no-hardcoded-volume', not vol_bad,
    f'hardcoded /vol2 in {vol_bad}' if vol_bad else 'volume-independent key lookup')

sp = os.path.join(app, 'sock_proxy.py')
sp_src = open(sp, encoding='utf-8', errors='replace').read() if os.path.exists(sp) else ''
chk('L0.sock-proxy', os.path.exists(sp))
chk('L0.host-hook-injection', '__STRATA_HOST__' in sp_src, 'sock_proxy injects official API-base hook')
chk('L0.iterative-prefix-strip', 'while True' in sp_src and 'rewrite_path' in sp_src, 'double-prefix safe')
proxy_markers = {
    'complete-request-body': 'forward_request_body' in sp_src,
    'clipped-body-carry': 'rest[take:]' in sp_src,
    'expect-100-answered': '100 Continue' in sp_src,
    'sse-keeps-chunked': 'Transfer-Encoding: chunked' in sp_src,
    'websocket-tunnel': 'pump_bidir' in sp_src,
    'forged-forwarded-dropped': 'x-forwarded-' in sp_src,
    'no-stream-timeout': 'settimeout(None)' in sp_src,
}
pmiss = [k for k, v in proxy_markers.items() if not v]
chk('L0.proxy-regression-markers', not pmiss,
    f'missing={pmiss}' if pmiss else f'{len(proxy_markers)}/{len(proxy_markers)} fixes present')

uicfg = os.path.join(app, 'ui', 'config')
utxt = open(uicfg, encoding='utf-8', errors='replace').read() if os.path.exists(uicfg) else ''
chk('L0.ui-title', '"Strata FNOS"' in utxt and '"title": "Strata"' not in utxt, f'ui/config={bool(utxt)}')

H = os.path.join(app, 'host')
markers = {
    'folder-picker.js': os.path.exists(os.path.join(H, 'dist', 'folder-picker.js')),
    'index.html-inject': '<script src="/folder-picker.js"></script>' in read('appx/host/dist/index.html'),
    'index.mjs-folderbridge': 'pickFolderBrowser' in read('appx/host/server/index.mjs'),
    'index.mjs-folder-routes': "path === '/api/folder-request/pending'" in read('appx/host/server/index.mjs') and "path === '/api/folder-request/resolve'" in read('appx/host/server/index.mjs'),
    'index.mjs-updates-sealed': 'fnos-port-managed' in read('appx/host/server/index.mjs'),
    'index.mjs-load-guard': 'resident.kind' in read('appx/host/server/index.mjs'),
    'engine.mjs-coerce': 'fnOS-port fix' in read('appx/host/server/engine.mjs'),
    'inference.mjs-probekey': 'STRATA_SERVER_KEY' in read('appx/host/server/inference.mjs') and 'authHeaders' in read('appx/host/server/inference.mjs'),
    'models.mjs-connected': 'connectedSynthetic' in read('appx/host/server/models.mjs'),
    'runtime-models.hasLoaded': 'hasLoaded' in read('appx/host/server/runtime-models.mjs'),
    'index.mjs.no-chooseFolder-remains': 'await chooseFolder(' not in read('appx/host/server/index.mjs'),
}
missing = [k for k, v in markers.items() if not v]
chk('L0.port-patches', not missing,
    f'missing={missing}' if missing else f'{len(markers)}/{len(markers)} markers')

ver = read('appx/control/VERSION').strip()
print(f'INFO | control VERSION = {ver}（官方组件版本，与 manifest {WANT_VER} 是两套编号，属正常）')

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print('ALL-PASS' if not fails else f'FAILED: {fails}')
sys.exit(0 if not fails else 1)
