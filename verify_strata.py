#!/usr/bin/env python3
"""Strata FNOS 打包静态自检（L0）+ 版本一致性断言。
用法: python3 verify_strata.py [fpk路径] [期望官方版本 0.2.3]
检查项:
  L0.manifest        appname/display/version 与期望一致
  L0.zero-inner-links fpk 内 app.tgz 无 symlink 条目
  L0.cmd-syntax      cmd/* 全部 bash -n 通过
  L0.sock-proxy      sock_proxy.py 存在
  L0.port-patches    host 补丁标记（fnOS-port / folder-picker / probe key）全部在位
  L0.official-version control VERSION 与 AppImage 组件一致性提示
"""
import sys, os, tarfile, subprocess, tempfile, json

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
    except FileNotFoundError:
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

# app.tgz 内层
tg = os.path.join(tmp, 'app.tgz')
links = 0
with tarfile.open(tg) as t:
    for m in t.getmembers():
        if m.issym() or m.islnk():
            links += 1
chk('L0.zero-inner-links', links == 0, f'link-entries={links}')

# 解 app.tgz 检查补丁
app = os.path.join(tmp, 'appx')
os.makedirs(app, exist_ok=True)
with tarfile.open(tg) as t:
    t.extractall(app)

cmd_dir = os.path.join(tmp, 'cmd')
bad = [f for f in os.listdir(cmd_dir)
       if subprocess.run(['bash', '-n', os.path.join(cmd_dir, f)]).returncode != 0]
chk('L0.cmd-syntax', not bad, f'bad={bad}')

sp = os.path.join(app, 'sock_proxy.py')
chk('L0.sock-proxy', os.path.exists(sp))

sp_src = open(sp, encoding='utf-8', errors='replace').read() if os.path.exists(sp) else ''
chk('L0.host-hook-injection', '__STRATA_HOST__' in sp_src, 'sock_proxy injects official API-base hook')
chk('L0.iterative-prefix-strip', 'while True' in sp_src and 'rewrite_path' in sp_src, 'double-prefix safe')

uicfg = os.path.join(app, 'ui', 'config')
utxt = open(uicfg, encoding='utf-8', errors='replace').read() if os.path.exists(uicfg) else ''
chk('L0.ui-title', '"Strata FNOS"' in utxt and '"title": "Strata"' not in utxt, f'ui/config={bool(utxt)}')

H = os.path.join(app, 'host')
markers = {
    'folder-picker.js': os.path.exists(os.path.join(H, 'dist', 'folder-picker.js')),
    'index.html-inject': '<script src="/folder-picker.js"></script>' in read('appx/host/dist/index.html'),
    'index.mjs-folderbridge': 'pickFolderBrowser' in read('appx/host/server/index.mjs'),
    'engine.mjs-coerce': 'fnOS-port fix' in read('appx/host/server/engine.mjs'),
    'inference.mjs-probekey': 'STRATA_SERVER_KEY' in read('appx/host/server/inference.mjs') and 'authHeaders' in read('appx/host/server/inference.mjs'),
    'models.mjs-connected': 'connectedSynthetic' in read('appx/host/server/models.mjs'),
    'runtime-models.hasLoaded': 'hasLoaded' in read('appx/host/server/runtime-models.mjs'),
    'index.mjs.no-chooseFolder-remains': 'await chooseFolder(' not in read('appx/host/server/index.mjs'),
}
missing = [k for k, v in markers.items() if not v]
chk('L0.port-patches', not missing, f'missing={missing}' if missing else f'{len(markers)}/7 markers')

ver = read('appx/control/VERSION').strip()
print(f'INFO | control VERSION = {ver}（官方组件版本，与 manifest {WANT_VER} 是两套编号，属正常）')

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print('ALL-PASS' if not fails else f'FAILED: {fails}')
sys.exit(0 if not fails else 1)
