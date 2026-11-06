# Strata on fnOS — 移植层

> **状态提示**：仓库截图来自历史版本，仅示意。`fnos-v0.2.10` 目前**只有静态与黑盒回归，没有 VM/实机回归**（提交说明里的 "VM regression still BLOCKED" 仍是实情）。发布前请按本文「自验」全绿 + 实机覆盖安装验证。

这是 [Strata](https://github.com/Niko1221/Strata) 在 **fnOS（飞牛私有云）** 上的 fpk 移植层。
不含上游源码改动：fnOS 安装的是上游 release 的预构建产物（AppImage 剥壳 + engine/control tar.gz），
本仓库只包含让它在 fnOS 应用中心里正确安装、启动、走网关、被生命周期管理的**外壳**。

## 目录

```
├── manifest              # fpk 源元数据（成品的 checksum 由 fnpack 生成）
├── cmd/                  # 生命周期脚本（start/stop/status/install/upgrade/uninstall）
├── config/               # privilege / resource 声明
├── ui/                   # 应用中心入口配置 + 图标
├── port-patches/         # 贴到上游 host/server/*.mjs 的补丁 + 不变量兜底脚本
├── tools/verify_proxy.py # sock_proxy 黑盒回归（13 场景，无需 fnOS/Node/GPU）
├── sock_proxy.py         # fnOS 网关 Unix socket → 本机端口的代理
├── delink_tree.py        # 打包前把软/硬链接实体化
├── rebuild_strata.sh     # 一键重建 fpk
└── verify_strata.py      # 成品 fpk 静态自检
```

`sock_proxy.py` 做的事：重写请求行 + Host、注入上游官方 `window.__STRATA_HOST__` 钩子、重写 HTML/JS/CSS 里的根绝对路径、完整转发请求体（CL/chunked/pipelining）、自答 `Expect: 100-continue`、WebSocket 全双工隧道、剥离浏览器 Origin/Referer 与客户端伪造的 `X-Forwarded-*`、SSE 保留 chunked 分帧且流式阶段无超时。
**它不注入 `X-Forwarded-Prefix`** —— Strata 的 host 不读这个头，SPA base 靠钩子 + 字面量重写解决。

## 打包

`app/` 载荷不在 git 里（体积 >1.7GB，为上游二进制）。打包步骤：

1. 从上游 release 下载对应硬件的产物（v0.2.10 实测资产名）：
   `Strata-<ver>-linux-x86_64.AppImage`、`strata-control-<cver>-linux-x86_64.tar.gz`、
   `strata-engine-<cver>-linux-x86_64-cuda-cuda13.tar.gz`、`SHA256SUMS.txt`
   （历史命名 `SHA256SUMS-<ver>.txt` / `-linux-update.AppImage` 仍兼容）
2. 按 `cmd/main` 内的路径约定布置 `app/{node,host,harness,engine-bundle,control,sock_proxy.py,ui}`
3. `fnpack build --directory strata-app`
4. **fnOS 安装器硬约束**：`app.tgz` 内不得含任何软链接**或硬链接**条目，否则报"解压app.tgz失败"；`delink_tree.py` 两种都处理，`verify_strata.py` 两种都查
5. **成品 manifest 必须带 `checksum = MD5(app.tgz)`**，包根必须有 `ICON.PNG` / `ICON_256.PNG`；缺任一项应用中心直接拒装（HermesFNOS 0.22.7.2 踩过）

## 关键约束（踩过的坑）

- fnOS 网关**不剥路径前缀**：代理必须自己重写请求行 + Host
- 浏览器 Origin/Referer 带网关域名会触发上游 loopback 防重绑定 → 代理必须剥离这两个头
- SSE 必须是**带 `Transfer-Encoding: chunked` 头的原始分帧流**：只转发 chunk 帧却剥掉 TE 头，浏览器会把 size 行当事件数据，跨 chunk 的事件会丢 →「引擎未连接」横幅
- 流式阶段（SSE/WS/下载）不能继承 socket 超时；超时只用于请求/响应头阶段
- fnOS glibc 2.36：选上游构建前先确认它的 glibc 门槛
- 同版本号覆盖安装不一定刷新文件：改了载荷必须递增 `manifest` 的 `version`
- 图标读取路径：应用中心读 `app.tgz` 内 `ui/images/icon_*.png` 与包根 `ICON.PNG`/`ICON_256.PNG`
- `cmd/main status` 未运行时退出码必须是 `3`；现在还要求端口已监听 + socket 存在
- 上游自升级（应用中心外）需封锁/说明，升级走应用中心覆盖安装
- 上游漂移：`host_server_index.mjs.patch` 在 0.2.10 已有 2/9 hunk 失败，由 `repair_router_invariants.py` 兜底。`rebuild_strata.sh` 现在只允许这一个补丁走兜底，且兜底后必须逐项过 marker 断言，其它补丁失败立即硬失败

## 运行时

- 面板端口规划 `18095-18099`，只绑 `127.0.0.1`
- 引擎（OpenAI 兼容 `/v1`）默认端口 `8080`；面板启动时探测 `http://127.0.0.1:8080/health`
  的 `"service":"strata"` 指纹，已有引擎则连接（A 模式），否则自行加载（native 模式）
- API key **不是随机生成**：`install_callback` 以 root 从 trim.hermes 的
  `/volN/@appdata/trim.hermes/workspace/strata/strata-*.json`（跨卷扫描，不写死 `/vol2`）
  读明文 key 并复制到应用数据目录 `server_key`（0600）；`cmd/main` 同样跨卷扫描。
  `uninstall_callback` 会删掉这个明文文件和 `components/` 缓存，保留用户 state/workspace。
- **native 模式待实机确认**：`config/privilege` 目前只声明 `video`/`render` 组，没有显式的 NVIDIA 设备直通。若机器没装 trim.hermes、降级到自带引擎，需实机确认 GPU 是否可见；在确认 fnOS 的设备声明格式前不擅自加字段。

## 许可

上游 Strata 的许可约束适用于所有二进制产物；本移植层脚本按上游仓库同等条款提供。

## 版本策略（2026-10-23 起）

- manifest `version` **必须等于上游官方版本号**（如上游 v0.2.3 → manifest 0.2.3），不再自造 0.2.1x 修订段。
- 历史遗留：曾发过自造的 0.2.17 / 0.2.18 包，fnOS 按数字段比较会判 0.2.17 ≥ 0.2.3，直接拦截覆盖安装（实测 VM 报"已安装相同或更高版本"）。已装旧号的机器升级 0.2.3 需先卸载再安装（数据目录 /vol1/@apphome|@appdata/strata-app 会被保留）。
- 组件内部版本（control 0.1.56 / engine 0.1.56 / harness）与 manifest 号是两套编号，属正常。

## 升级配方（一键）

```bash
# 原料放 dl/：SHA256SUMS.txt + Strata-<VER>-linux-x86_64.AppImage
#             + strata-engine-*-cuda-cuda13.tar.gz + strata-control-*.tar.gz
./rebuild_strata.sh <VER>
```

流程: SHA256 校验 → 解 AppImage（control/harness/host/node；engine 用独立 cuda13 tar，目录名 `<cver>-linux-x86_64-cuda-cuda13`）→ host 重贴 port-patches/ 补丁 + folder-picker.js + index.html 注入 → 不变量兜底与全部 marker 断言 → **同步仓库 sock_proxy.py 进载荷** → 软/硬链接实体化 → manifest=官方版本 → 代理黑盒回归 → fnpack → verify_strata.py 全绿才算过。

## 自验

```bash
python3 tools/verify_proxy.py                      # 13/13 PASS（黑盒，约 15s）
python3 verify_strata.py strata-app.fpk <VER>     # 成品：manifest/checksum/图标/链接/cmd/补丁标记
```

`verify_strata.py` 检查项：manifest 三字段、`checksum == MD5(app.tgz)`、包根双图标、app.tgz 零软/硬链接、cmd 语法、版本一致、无 `/vol2` 硬编码、sock_proxy 的 7 项回归修复标记、11 项 host 补丁标记。

## 0.2.10 审计修复（本 PR）

实锤并修复：SSE chunked 分帧被破坏、请求体 carry 未裁剪导致 pipelining 串包、`Expect: 100-continue` 死锁；`rebuild` 对 HUNK-PARTIAL 静默放行、`SHA256SUMS-<VER>.txt` 名字对不上上游；`/vol2` 硬编码；`delink_tree.py` 不处理硬链接；`verify` 缺 checksum / 包根图标 / marker 计数写死；README 与实现不符（X-Forwarded-Prefix、随机 key、AppImage 资产名）。
顺带补：WebSocket 升级直通、伪造转发头过滤、通用 `/app/<段>/` 前缀误剥、流式阶段超时、`status()` 只查 pidfile、host 未绑端口仍报启动成功、卸载残留明文 key 与组件缓存、构建时未同步代理源码。

**未改 / 待实机**：`config_callback` 保持 no-op（隐式重启会打断会话，等确认 fnOS 回调语义再动）；`config/privilege` 的 GPU 设备直通；上游 `index.mjs` 补丁漂移的根治（重贴到 0.2.10 基线）；以及全链路 VM 覆盖安装 + GPU 实跑。
