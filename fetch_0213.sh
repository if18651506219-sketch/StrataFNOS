#!/bin/bash
# fetch Strata 0.2.13 assets (resume+retry), then rebuild + verify. Nightly cron chain.
W=/vol2/@apphome/hermes-si/data/hermes-home/port/strata-work
cd "$W" || exit 1
B=https://github.com/Niko1221/Strata/releases/download/v0.2.13
if [ ! -s dl/SHA256SUMS-0.2.13.txt ]; then
  curl -fsSL --connect-timeout 20 -o dl/SHA256SUMS-0.2.13.txt "$B/SHA256SUMS.txt" || { echo DL-FAIL-SUMS; exit 1; }
fi
fetch() {
  f="$1"
  i=0
  (cd dl && grep -F "$f" SHA256SUMS-0.2.13.txt | grep -v '.sig$' | sha256sum -c - >/dev/null 2>&1) && return 0
  while [ "$i" -lt 200 ]; do
    curl -L -C - --connect-timeout 15 --speed-time 180 --speed-limit 500 -o "dl/$f" "$B/$f" && return 0
    sleep 10
    i=$((i+1))
  done
  return 1
}
fetch Strata-0.2.13-linux-x86_64.AppImage || { echo DL-FAIL-APPIMAGE; exit 1; }
echo APPIMAGE-OK
fetch strata-engine-0.1.59-linux-x86_64-cuda-cuda13.tar.gz || { echo DL-FAIL-ENGINE; exit 1; }
echo ENGINE-OK
fetch strata-control-0.1.59-linux-x86_64.tar.gz || { echo DL-FAIL-CONTROL; exit 1; }
echo CONTROL-OK
./rebuild_strata.sh 0.2.13 || { echo REBUILD-FAIL; exit 1; }
echo PIPELINE-DONE
