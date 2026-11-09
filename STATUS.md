# StrataFNOS 0.2.13 交接（2026-11-10 夜间 cron）

## 已完成
- 上游 v0.2.13（2026-11-09），control/engine 组件 0.1.59；release 并发 control 0.1.59.1，
  rebuild 的 sort -V 自动选 .1 覆盖 AppImage 内嵌 0.1.59（正常双编号）。
- dl/ 四资产 SHA256 全 OK（凌晨直连 GitHub 1-20MB/s，未走代理）。
  fetch_0213.sh 首跑有 bug（fetch() 首参写成字面量）已修复重跑。
- rebuild_strata.sh 0.2.13 全链跑通：host 补丁 6/6 CLEAN-APPLY（零 HUNK-PARTIAL）、
  chooseFolder 残留=0、engine-bundle 目录名=0.1.59-linux-x86_64-cuda-cuda13（无前缀）、
  sock_proxy 回归 13/13 PASS、verify_strata.py ALL-PASS（L0 13 项含 ui-title=Strata FNOS、
  host-hook-injection、iterative-prefix-strip、port-patches 11/11、app-version=0.2.13）。
- test 包：/fs/1000/ftp/H空间/01-fpk安装包/strata-fnos-test/StrataFNOS-0.2.13-test.fpk
  md5 = 68b52ea818db8a2b6c8dc6b65ffc28a4（965,525,402 B）
- git commit 2a4448d 已 push origin/main（走本机代理 127.0.0.1:7897）。

## 未完成（两步制铁律，转正前必须）
1. 用户真机覆盖安装 test 包（0.2.13 > 已装版本，直接覆盖，配置保留）。
   验收要点：应用中心版本号=0.2.13；界面无"未连接"横幅；发消息有回复。
2. 用户实测发话 → 转正：cp strata-app.fpk → /fs/1000/ftp/H空间/01-fpk安装包/StrataFNOS.fpk、
   清 test 文件、git tag fnos-v0.2.13（代理 push）+ 建 Release 传附件 StrataFNOS.fpk。

## Hermes 侧本次无动作
- 上游 hermes-agent releases/latest = v0.23.0（2026-11-05），真 NAS 已装 0.23.0.2
  （GitHub fnos-v0.23.0.2 tag/Release 已在，H空间 hermes-fnos/ 有正式包），无需新包。

## VM 说明
- VM 自测回归已取消（用户 11-05 令，两步制 test 流程）。
