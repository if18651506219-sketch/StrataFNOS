#!/bin/bash
# Strata FNOS 升级重建流程（2026-10-29 迁移版：新工作区 port/strata-work，系统 fnpack，上游资产改名适配）
# 原料（dl/，来自上游 release）:
#   - SHA256SUMS-<VER>.txt
#   - Strata-<VER>-linux-x86_64.AppImage  (0.2.7 起改名，旧名 -linux-update.AppImage 仍兼容)
#   - strata-engine-<CD>-linux-x86_64-cuda-cuda13.tar.gz
# 用法: ./rebuild_strata.sh <VER>
set -u
W=/vol2/@apphome/hermes-si/data/hermes-home/port/strata-work
FNPACK=/usr/local/bin/fnpack
VER="$1"
cd "$W" || exit 1

echo "== 1. SHA256 校验"
AGEB_VER="$VER"
{ grep -q "Strata-$VER-linux-x86_64.AppImage" "dl/SHA256SUMS-$VER.txt" || grep -q "Strata-$VER-linux-update.AppImage" "dl/SHA256SUMS-$VER.txt"; } || {
  [ "$VER" != "${VER%.*}" ] && [ "$(awk -F. '{print NF}' <<<"$VER")" -ge 4 ] && AGEB_VER="${VER%.*}"
}
echo "asset-name-version=$AGEB_VER (manifest $VER)"
grep -E "Strata-$AGEB_VER-linux-(x86_64|update)\.AppImage|strata-engine-.*linux-x86_64-cuda-cuda13\.tar\.gz|strata-control-.*linux-x86_64\.tar\.gz" "dl/SHA256SUMS-$VER.txt" | grep -v '.sig$' > dl/want.sums || exit 1
(cd dl && sha256sum -c want.sums) || { echo SHA256-FAIL; exit 1; }

echo "== 2. 解 update AppImage（组件源）"
APPIMG="Strata-$AGEB_VER-linux-x86_64.AppImage"
[ -f "dl/$APPIMG" ] || APPIMG="Strata-$AGEB_VER-linux-update.AppImage"
rm -rf upimg
mkdir -p upimg
chmod +x "dl/$APPIMG"
(cd upimg && "../dl/$APPIMG" --appimage-extract >/dev/null 2>&1) || { echo EXTRACT-FAIL; exit 1; }
S=upimg/squashfs-root/usr/lib/Strata
[ -d "$S/control" ] || { echo "NO-STRATA-COMPONENTS"; exit 1; }
echo "components: $(ls $S | tr '\n' ' ') | control VERSION: $(cat $S/control/VERSION)"
ENGPKG=$(grep -oE 'strata-engine-[0-9.]+-linux-x86_64-cuda-cuda13' dl/want.sums | sort -V | tail -1)
[ -n "$ENGPKG" ] || { echo 'NO-ENGINE-PKG'; exit 1; }
VERDIR=${ENGPKG#strata-engine-}

echo "== 3. 重建 payload"
A=strata-app/app
for c in control harness node; do
  rm -rf "$A/$c"; cp -a "$S/$c" "$A/$c"
done
CTLTAR=$(grep -oE 'strata-control-[0-9.]+-linux-x86_64' dl/want.sums | sort -V | tail -1)
if [ -n "$CTLTAR" ] && [ -f "dl/${CTLTAR}.tar.gz" ]; then
  echo "== 3b. control 覆盖安装 $CTLTAR (AppImage 内嵌为 $(cat $A/control/VERSION))"
  rm -rf "$A/control"; mkdir -p "$A/control"
  tar -xzf "dl/${CTLTAR}.tar.gz" -C "$A/control" || { echo CONTROL-EXTRACT-FAIL; exit 1; }
  echo "control VERSION -> $(cat $A/control/VERSION)"
fi
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
  patch --no-backup-if-mismatch -l "$tgt" "$p" >/dev/null 2>&1 || { echo "HUNK-PARTIAL: $p (fallback 接管)"; FAILHUNK=1; }
done
[ -e "$A/host/server/index.mjs.rej" ] && rm -f "$A/host/server/index.mjs.rej"
python3 port-patches/repair_router_invariants.py "$A/host/server/index.mjs" || { echo 'ROUTER-REPAIR-FAIL'; exit 1; }
node --check "$A/host/server/index.mjs" || { echo 'INDEX-MJS-SYNTAX-FAIL'; exit 1; }
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

echo "== 4b. 载荷链接实体化"
python3 delink_tree.py "$A" || exit 1

echo "== 5. manifest 版本 = 官方 $VER"
sed -i "s/^version               = .*/version               = $VER/" strata-app/manifest
sed -i "s/(Strata [0-9.]*, NVIDIA/(Strata $VER, NVIDIA/" strata-app/manifest
sed -i "s/STRATA_APP_VERSION=\"[0-9][0-9.]*\"/STRATA_APP_VERSION=\"$VER\"/" strata-app/cmd/main
grep -q "STRATA_APP_VERSION=\\\"$VER\\\"" strata-app/cmd/main || { echo 'APP-VERSION-SED-FAIL'; exit 1; }

echo "== 6. fnpack 打包"
rm -f strata-app.fpk
"$FNPACK" build --directory strata-app >/dev/null 2>&1 || { echo FNPACK-FAIL; "$FNPACK" build --directory strata-app; exit 1; }
[ -f "$W/strata-app.fpk" ] || { echo "NO-FPK-OUTPUT (check cwd)"; ls -la "$W"/strata-app.fpk $(pwd)/strata-app.fpk 2>/dev/null; exit 1; }

echo "== 7. 静态自检"
python3 verify_strata.py strata-app.fpk "$VER" || exit 1
echo "BUILD-OK $(md5sum strata-app.fpk)"
