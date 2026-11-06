#!/bin/bash
# Strata FNOS 升级重建流程（2026-10-29 迁移版：新工作区 port/strata-work，系统 fnpack，上游资产改名适配）
# 原料（dl/，来自上游 release）:
#   - SHA256SUMS.txt（上游正式名）或 SHA256SUMS-<VER>.txt（历史命名，两者兼容）
#   - Strata-<VER>-linux-x86_64.AppImage  (0.2.7 起改名，旧名 -linux-update.AppImage 仍兼容)
#   - strata-engine-<CD>-linux-x86_64-cuda-cuda13.tar.gz
#   - strata-control-<CD>-linux-x86_64.tar.gz
# 用法: ./rebuild_strata.sh <VER>
set -u
W="${STRATA_BUILD_ROOT:-/vol2/@apphome/hermes-si/data/hermes-home/port/strata-work}"
FNPACK=/usr/local/bin/fnpack
VER="$1"
cd "$W" || exit 1

echo "== 1. SHA256 校验"
SUMS="dl/SHA256SUMS-$VER.txt"
[ -f "$SUMS" ] || SUMS="dl/SHA256SUMS.txt"
[ -f "$SUMS" ] || { echo "NO-SHA256SUMS: dl/SHA256SUMS*.txt"; exit 1; }
echo "sums-file=$SUMS"
AGEB_VER="$VER"
{ grep -q "Strata-$VER-linux-x86_64.AppImage" "$SUMS" || grep -q "Strata-$VER-linux-update.AppImage" "$SUMS"; } || {
  [ "$VER" != "${VER%.*}" ] && [ "$(awk -F. '{print NF}' <<<"$VER")" -ge 4 ] && AGEB_VER="${VER%.*}"
}
echo "asset-name-version=$AGEB_VER (manifest $VER)"
grep -E "Strata-$AGEB_VER-linux-(x86_64|update)\.AppImage|strata-engine-.*linux-x86_64-cuda-cuda13\.tar\.gz|strata-control-.*linux-x86_64\.tar\.gz" "$SUMS" | grep -v '.sig$' > dl/want.sums || exit 1
[ -s dl/want.sums ] || { echo 'EMPTY-WANT-SUMS (asset names drifted?)'; exit 1; }
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
# 0.2.10 修复：旧版对 patch 失败只打印 HUNK-PARTIAL、置 FAILHUNK=1 却从不检查，
# 上游再漂一次就会静默打出缺路由的坏包。现在只有 index.mjs 允许走
# repair_router_invariants.py 兜底，且兜底后必须过全部 marker 断言；其它文件
# 的补丁一旦应用失败立即硬失败（它们没有兜底脚本）。
FAILED_PATCHES=""
for p in port-patches/host_server_*.patch; do
  base=$(basename "$p" .patch)
  tgt="$A/host/$(echo "${base#host_}" | sed 's/_/\//')"
  [ -f "$tgt" ] || { echo "PATCH-TARGET-MISSING: $tgt"; exit 1; }
  if patch --dry-run -l "$tgt" "$p" >/dev/null 2>&1; then
    patch --no-backup-if-mismatch -l "$tgt" "$p" >/dev/null 2>&1 || {
      echo "PATCH-APPLY-FAIL: $p"; exit 1; }
  elif [ "$base" = "host_server_index.mjs" ]; then
    echo "HUNK-PARTIAL: $p (index.mjs -> repair_router_invariants fallback)"
    FAILED_PATCHES="$FAILED_PATCHES $base"
    patch --no-backup-if-mismatch -l "$tgt" "$p" >/dev/null 2>&1 || true
  else
    echo "PATCH-FAIL: $p (no fallback for this file)"; exit 1
  fi
done
find "$A/host" -type f \( -name '*.rej' -o -name '*.orig' \) -delete
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
# 所有补丁的语义 marker 必须在打包前逐项确认（不再只靠 verify 事后字符串匹配）
for marker in "pickFolderBrowser" "'/api/folder-request/pending'" "'/api/folder-request/resolve'" \
              "fnos-port-managed" "resident.kind"; do
  grep -qF "$marker" "$A/host/server/index.mjs" || { echo "MARKER-MISS index.mjs: $marker"; exit 1; }
done
grep -qF 'fnOS-port fix' "$A/host/server/engine.mjs" || { echo 'MARKER-MISS engine.mjs'; exit 1; }
grep -qF 'STRATA_SERVER_KEY' "$A/host/server/inference.mjs" || { echo 'MARKER-MISS inference.mjs'; exit 1; }
grep -qF 'connectedSynthetic' "$A/host/server/models.mjs" || { echo 'MARKER-MISS models.mjs'; exit 1; }
grep -qF 'hasLoaded' "$A/host/server/runtime-models.mjs" || { echo 'MARKER-MISS runtime-models.mjs'; exit 1; }
grep -q 'await chooseFolder(' "$A/host/server/index.mjs" && { echo 'CHOOSEFOLDER-REMAINS'; exit 1; }
[ -n "$FAILED_PATCHES" ] && echo "HUNK-FALLBACK-USED:$FAILED_PATCHES (all marker asserts passed)"

# 0.2.10 修复：显式把仓库里的代理同步进载荷。旧流程假设包目录里的
# sock_proxy.py 已被手工更新，改了仓库源码却打出旧代理是静默回归。
cp sock_proxy.py "$A/sock_proxy.py" || exit 1

echo "== 4b. 载荷链接实体化（软链接 + 硬链接）"
python3 delink_tree.py "$A" || exit 1

echo "== 5. manifest 版本 = 官方 $VER"
sed -i "s/^version               = .*/version               = $VER/" strata-app/manifest
sed -i "s/(Strata [0-9.]*, NVIDIA/(Strata $VER, NVIDIA/" strata-app/manifest
sed -i "s/STRATA_APP_VERSION=\"[0-9][0-9.]*\"/STRATA_APP_VERSION=\"$VER\"/" strata-app/cmd/main
grep -q "STRATA_APP_VERSION=\\\"$VER\\\"" strata-app/cmd/main || { echo 'APP-VERSION-SED-FAIL'; exit 1; }

echo "== 5b. 代理黑盒回归（13 场景，STRATA_SKIP_PROXY_TEST=1 可跳过）"
if [ "${STRATA_SKIP_PROXY_TEST:-0}" != "1" ]; then
  STRATA_PROXY_SRC="$W/$A/sock_proxy.py" python3 tools/verify_proxy.py || { echo 'PROXY-REGRESSION-FAIL'; exit 1; }
fi

echo "== 6. fnpack 打包"
rm -f strata-app.fpk
"$FNPACK" build --directory strata-app >/dev/null 2>&1 || { echo FNPACK-FAIL; "$FNPACK" build --directory strata-app; exit 1; }
[ -f "$W/strata-app.fpk" ] || { echo "NO-FPK-OUTPUT (check cwd)"; ls -la "$W"/strata-app.fpk $(pwd)/strata-app.fpk 2>/dev/null; exit 1; }

echo "== 7. 成品静态自检（含 manifest.checksum / 包根图标）"
python3 verify_strata.py strata-app.fpk "$VER" || exit 1
echo "BUILD-OK $(md5sum strata-app.fpk)"
