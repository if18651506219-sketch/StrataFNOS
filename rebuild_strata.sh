#!/bin/bash
# Strata FNOS 升级重建流程（可重复；夜间 cron 走这条链）
# 原料（dl/，来自上游 v<VER> GitHub release）:
#   - SHA256SUMS-<VER>.txt
#   - Strata-<VER>-linux-update.AppImage     (control/harness/host/node, 不含引擎)
#   - strata-engine-<CD>-linux-x86_64-cuda-cuda13.tar.gz  (CUDA13 引擎)
# 用法: ./rebuild_strata.sh <VER> [dl目录里的engine包文件名]
set -u
W=/vol2/@apphome/hermes-agent/data/workspace/strata-fpk
VER="$1"
cd "$W" || exit 1

echo "== 1. SHA256 校验"
grep -E "Strata-$VER-linux-update.AppImage|strata-engine-.*linux-x86_64-cuda-cuda13.tar.gz|strata-control-.*linux-x86_64.tar.gz" dl/SHA256SUMS-$VER.txt > dl/want.sums || exit 1
(cd dl && sha256sum -c want.sums) || { echo SHA256-FAIL; exit 1; }

echo "== 2. 解 update AppImage（组件源）"
rm -rf upimg
mkdir -p upimg
chmod +x "dl/Strata-$VER-linux-update.AppImage"
(cd upimg && "../dl/Strata-$VER-linux-update.AppImage" --appimage-extract >/dev/null 2>&1) || { echo EXTRACT-FAIL; exit 1; }
S=upimg/squashfs-root/usr/lib/Strata
[ -d "$S/control" ] || { echo "NO-STRATA-COMPONENTS"; exit 1; }
echo "components: $(ls $S | tr '\n' ' ') | control VERSION: $(cat $S/control/VERSION)"
ENGPKG=$(grep -oE 'strata-engine-[0-9.]+-linux-x86_64-cuda-cuda13' dl/want.sums | head -1)
[ -n "$ENGPKG" ] || { echo 'NO-ENGINE-PKG'; exit 1; }
VERDIR=${ENGPKG#strata-engine-}

echo "== 3. 重建 payload"
A=strata-app/app
for c in control harness node; do
  rm -rf "$A/$c"; cp -a "$S/$c" "$A/$c"
done
rm -rf "$A/engine-bundle"
mkdir -p "$A/engine-bundle/$VERDIR"
tar -xzf "dl/${ENGPKG}.tar.gz" -C "$A/engine-bundle/$VERDIR" || { echo ENGINE-EXTRACT-FAIL; exit 1; }

echo "== 4. host = 原始 + 重贴 fnOS 补丁"
rm -rf "$A/host"; cp -a "$S/host" "$A/host"
cp port-patches/folder-picker.js "$A/host/dist/folder-picker.js"
grep -q 'folder-picker.js' "$A/host/dist/index.html" || sed -i 's#<div id="root"></div>#<div id="root"></div>\n    <script src="/folder-picker.js"></script>#' "$A/host/dist/index.html"
FAILHUNK=0
for p in port-patches/host_server_*.patch; do
  base=$(basename "$p" .patch)
  tgt="$A/host/$(echo "${base#host_}" | sed 's/_/\//')"
  # 容忍部分 hunk 失败（会留 .rej）；由后面的不变量检查兜底
  patch --no-backup-if-mismatch -l "$tgt" "$p" >/dev/null 2>&1 || echo "HUNK-PARTIAL: $p (fallback 接管)"
done
[ -e "$A/host/server/index.mjs.rej" ] && rm -f "$A/host/server/index.mjs.rej"
# 兜底不变量: headless 环境不许残留 desktop chooseFolder 调用
LEFT=$(grep -c 'await chooseFolder(' "$A/host/server/index.mjs" || true)
if [ "$LEFT" -gt 0 ]; then
  python3 - "$A/host/server/index.mjs" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
s = s.replace('await chooseFolder(', 'await pickFolderBrowser(')
open(p, 'w').write(s)
PY
  echo "FALLBACK-BRIDGE: $LEFT chooseFolder -> pickFolderBrowser"
fi
grep -q 'pickFolderBrowser' "$A/host/server/index.mjs" || { echo 'MARKER-MISS index.mjs'; exit 1; }
[ "$(grep -c 'await chooseFolder(' "$A/host/server/index.mjs" || true)" -eq 0 ] || { echo 'CHOOSEFOLDER-REMAINS'; exit 1; }

echo "== 5. manifest 版本 = 官方 $VER"
sed -i "s/^version               = .*/version               = $VER/" strata-app/manifest
sed -i "s/(Strata [0-9.]*, NVIDIA/(Strata $VER, NVIDIA/" strata-app/manifest

echo "== 6. fnpack 打包"
rm -f strata-app.fpk
./fnpack build --directory strata-app >/dev/null 2>&1 || { echo FNPACK-FAIL; exit 1; }
[ -f "$W/strata-app.fpk" ] || { echo "NO-FPK-OUTPUT (check cwd)"; ls -la "$W"/strata-app.fpk $(pwd)/strata-app.fpk 2>/dev/null; exit 1; }

echo "== 7. 静态自检"
python3 verify_strata.py strata-app.fpk "$VER" || exit 1
echo "BUILD-OK $(md5sum strata-app.fpk)"
