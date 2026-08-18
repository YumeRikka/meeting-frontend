# 会议机器人迁移到 NAS · 详细迁移文档

> 适用：把当前 **Windows 本机** 跑的「Flask 后端 + Playwright 自动建会 + 5 个腾讯会议账号登录态」整体搬到 **NAS（Docker）**，并让对外域名 `api.rikka.com.cn` 改指 NAS。  
> 前端（GitHub Pages `meet.rikka.com.cn`）**不动**，因为 `config.prod.js` 本来就指向 `api.rikka.com.cn`，只要后端地址不变，前端零改动。

---

## 0. 目标、范围与边界

### 当前架构（迁移前）

```
本机 Windows
 ├─ Flask 后端 (h5/app.py)  ← 监听 localhost:5000
 ├─ Playwright + 5 账号登录态 (python/profiles/)
 ├─ 凭证：python/.env + python/accounts.json
 ├─ 卡密库：h5/cards.db
 └─ Cloudflare Tunnel (cloudflared)  ← api.rikka.com.cn → localhost:5000
前端：GitHub Pages meet.rikka.com.cn → fetch(api.rikka.com.cn)
可选：微信群机器人 meeting_bot.py（wxauto4，需 Windows 微信 PC）
```

### 目标架构（迁移后）

```
NAS (Docker)
 ├─ 容器 meeting-bot：Flask 5000 + Playwright + 5 账号登录态
 ├─ 凭证/数据：deploy/config/ + deploy/data/（挂载，不进镜像）
 └─ 容器 cloudflared：api.rikka.com.cn → 本机 5000
前端：GitHub Pages meet.rikka.com.cn → fetch(api.rikka.com.cn)  ← 不变
Windows 可关机（除非还要用微信群机器人，见 §8）
```

### 明确边界

- **能上 NAS 的**：Flask 后端、Playwright 自动建会、5 账号网页登录态、卡密库、Tunnel。
- **不能上 NAS 的**：微信群机器人（`meeting_bot.py` + `wxauto4`）——它依赖 Windows 上已登录的**微信 PC 客户端**，Linux/NAS 无法运行。处理方案见 §8。
- **不动的**：GitHub Pages 前端、`config.prod.js`、Cloudflare 上的 DNS 记录（`api.rikka.com.cn` / `meet.rikka.com.cn` 的 CNAME 都不用改）。

---

## 1. 前置条件

- [ ] NAS 已安装 **Docker**（应用中心里装，或 SSH 后 `docker -v` 验证）。
- [ ] 已开启 **SSH**（端口以你 NAS 实际为准），或能用 NAS 的「终端/File Station」。
- [ ] 你有一个域名托管在 **Cloudflare**，且 `api.rikka.com.cn` 已是它名下的记录。
- [ ] 本机 Windows 上的 `meeting-bot` 仓库是**完整最新**的（含尚未推送的修复提交，如「会议信息返回加主持人密钥」那条）。⚠️ 这点很重要，见 §3 警告。

---

## 2. 迁移清单（要搬什么，落到哪）

| 资产                               | Windows 位置                  | NAS 目标位置                      | 必须？   | 备注                                        |
| -------------------------------- | --------------------------- | ----------------------------- | ----- | ----------------------------------------- |
| 代码（python/ h5/ deploy/ .github/） | `D:\WorkBuddy\meeting-bot\` | `<NAS_PROJECT_DIR>/`     | ✅     | 用复制或先 push 再 clone（见 §3）                  |
| 账号密码 `accounts.json`             | `python/accounts.json`      | `deploy/config/accounts.json` | ✅     | 5 个账号、含密码                                 |
| 环境变量 `.env`                      | `python/.env`               | `deploy/config/.env`          | ✅     | 腾讯会议 REST 凭证 + `HOST_KEY` + `ADMIN_TOKEN` |
| 卡密库 `cards.db`                   | `h5/cards.db`               | `deploy/data/cards.db`        | ✅     | **不搬会丢失全部卡密与已用次数**                        |
| 登录态 `profiles/`                  | `python/profiles/`          | `deploy/data/profiles/`       | ⚠️ 可选 | **不保证跨平台可用**，见 §4                         |
| Cloudflare Tunnel                | Windows 本机 cloudflared      | NAS 容器 cloudflared            | ✅     | 见 §6，必须停掉 Windows 那份                      |

> 路径说明：`<NAS_PROJECT_DIR>` 是**占位变量**，指你把 `meeting-bot` 项目放在 NAS 上的**真实绝对路径**。不同 NAS 默认挂载点不同（群晖通常 `/volume1/共享文件夹/...`、威联通 `/share/...`、飞牛/TrueNAS 类似 `/mnt/池名/...`）。请把下文所有 `<NAS_PROJECT_DIR>` 替换成你 NAS 上实际用来放 docker 项目的目录（例如 `/volume1/docker/meeting-bot`）。

---

## 3. 把代码放到 NAS（二选一）

### ⚠️ 重要警告：别直接 clone 旧代码

你最近的好几个修复（主持人密钥、admin 405、30 秒卡顿等）**还只在本机、没 push 到 GitHub**。如果现在在 NAS 上 `git clone`，拿到的会是一份**缺这些修复的旧代码**。

所以首迁推荐 **方式 A（直接复制本机目录）**，保证拿到当前最新状态。

### 方式 A：复制整个目录（推荐，首迁）

用 NAS 的 File Station 上传，或 SSH/SCP：

```bash
# 在能访问 NAS 的机器上（或 NAS 的 File Station 直接传文件夹）
scp -r -P <SSH_PORT> /d/WorkBuddy/meeting-bot root@<NAS_IP>:<NAS_PROJECT_DIR>
# 或：把 meeting-bot 整个文件夹拖进 File Station 的 <NAS_PROJECT_DIR> 下
```

复制后，NAS 上 `<NAS_PROJECT_DIR>/` 就是和本机一模一样的最新代码。

### 方式 B：先 push 再 clone（适合后续长期同步）

```bash
# 本机：把本地提交推上去（沙箱代理不可用，这一步在你本机做）
cd D:\WorkBuddy\meeting-bot
git push origin main

# NAS 上
ssh -p 10000 root@<NAS_IP>
git clone <你的仓库地址> <NAS_PROJECT_DIR>
```

> 以后升级代码走这条：`git pull` → `docker compose build` → `docker compose up -d`。

---

## 4. 放凭证与数据

```bash
ssh -p 10000 root@<NAS_IP>
cd <NAS_PROJECT_DIR>

# 建挂载目录
mkdir -p deploy/config deploy/data

# 凭证（从本机拷过来；若用方式 A 整体复制，这些已经在 deploy 外了，需单独归位）
cp python/accounts.json   deploy/config/accounts.json
cp python/.env           deploy/config/.env

# 卡密库（务必带过去，否则卡全部清零）
cp h5/cards.db           deploy/data/cards.db

# .env 含密钥，收紧权限（可选但建议）
chmod 600 deploy/config/.env
```

> 注意：`deploy/` 下的 `Dockerfile` / `docker-compose*.yml` 已配置把 `deploy/config/` 和 `deploy/data/` 挂载进容器，并**明确排除**在镜像与 git 之外（见 `.dockerignore`）。凭证不会进镜像、不会进仓库。

---

## 5. 构建并启动后端容器

```bash
cd <NAS_PROJECT_DIR>/deploy
docker compose build          # 首次构建会下载 python:3.13-slim + 装 Playwright Chromium，较慢
docker compose up -d
```

验证后端起来了：

```bash
docker logs -f meeting-bot   # 看有没有报错
curl -s http://<NAS_IP>:5000/ | head -5   # 或浏览器开 http://<NAS_IP>:5000
```

浏览器开 `http://<NAS_IP>:5000` 应能看到页面（同源可直接用，先不依赖外网）。

---

## 6. 5 个账号登录态

建会靠网页登录态。登录需要人眼扫码/输密码，所以走「虚拟桌面 noVNC」一次性完成。

> **5 个账号的正确 userid（注意 829 前缀，旧 README 漏写了会登不上）：**  
> `admin1783592634`、`wemeeting8280568`、`wemeeting8280570`、`wemeeting8295133`、`wemeeting8295134`  
> （与 `accounts.json` 里的 `userid` 字段一一对应。）

### 6.1 重新登录（最稳，推荐）

```bash
cd <NAS_PROJECT_DIR>/deploy
docker compose -f docker-compose.login.yml up -d
```

1. 浏览器开 `http://<NAS_IP>:6080` → 看到空虚拟桌面。
2. 另开 SSH 终端，对 5 个账号各执行一次：
   ```bash
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login admin1783592634
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280568
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280570
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295133
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295134
   ```
3. 每次执行后，回到 noVNC 桌面完成扫码 / 输密码登录（登录窗口可关，状态已落盘）。
4. 5 个全登完后，在启动 login 容器的终端按 `Ctrl+C`，然后：
   ```bash
   docker compose -f docker-compose.login.yml down
   docker compose up -d
   ```
   登录态已写入 `deploy/data/profiles/`，重启容器不丢。

### 6.2 迁移旧 profile（省事但有风险，不保证成功）

Windows 的 `python/profiles/` 是 **Windows 版 Chromium** 的用户目录，搬到 Linux 容器里的 Chromium **可能不兼容**（Playwright/Chromium 版本不一致、腾讯可能绑定设备指纹）。只有「想省掉 5 次扫码」时才试：

```bash
# 把本机 python/profiles/* 整个拷到 NAS deploy/data/profiles/
# 然后直接 docker compose up -d，实测用 H5 建一次会
```

- **成功**：直接能用，跳过 6.1。
- **失败**（报未登录 / 跳登录页 / 崩溃）：删掉 `deploy/data/profiles/`，回到 **6.1 重新登录**。这是预期内的兜底，不丢数据。

---

## 7. 把 Tunnel 搬到 NAS（关键，避免两台机器抢同一域名）

现在 `api.rikka.com.cn` 由 **Windows 上的 cloudflared** 提供。迁移后必须由 NAS 提供，否则：

- 两台都跑 → 域名在两台机器间乱跳，建会时灵时不灵；
- 只关 Windows 不启 NAS → 域名失联，前端全挂。

### 7.1 停掉 Windows 上的 cloudflared

在你 Windows 上停掉正在运行的 cloudflared（任务管理器结束进程 / 关掉对应的命令行窗口 / 或停掉对应的 Docker 容器）。**先停 Windows，再起 NAS**，中间有几分钟前端不可用是正常的。

### 7.2 在 NAS 上用 Docker 跑 cloudflared

推荐用 **命名隧道 + Token**（一条命令、可常驻、稳定）：

1. 在 Cloudflare 控制台创建一个 **Tunnel**，拿到它的 **Token**（一长串）。
2. NAS 上：
   ```bash
   docker run -d --name cloudflared --restart unless-stopped --network host \
     cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <你的TUNNEL_TOKEN>
   ```
   > `--network host` 让容器直接用 NAS 主机网络，`localhost:5000` 即 `meeting-bot` 容器映射到主机的 5000 端口。  
   > 隧道入口在 Cloudflare 控制台配置为 `https://api.rikka.com.cn` → `http://localhost:5000`。
   快速验证（不想建命名隧道时）也可临时用 `--url`：
   ```bash
   docker run -d --name cloudflared --restart unless-stopped --network host \
     cloudflare/cloudflared:latest tunnel --url http://localhost:5000
   ```
   但 `--url` 是临时随机隧道，域名会变，**不适合长期用**，仅用于先打通测试。

### 7.3 验证隧道命中 NAS

```bash
# 在任意机器
curl -s https://api.rikka.com.cn/ | head -5
# 或浏览器开 meet.rikka.com.cn 实测用卡密建一次会
```

并在 NAS 上看后端有没有收到请求：

```bash
docker logs -f meeting-bot
```

---

## 8. 微信群机器人（wxauto）的处理

> 当前版本已移除微信机器人（`meeting_bot.py` 已弃用，详见项目根 `python/requirements.txt` 标注），**本节仅供未来若恢复时参考，现在迁移无需任何微信机器人相关操作**。

`meeting_bot.py` 依赖 **Windows 上已登录的微信 PC + wxauto4 库**，这套**无法在 Linux/NAS 跑**，所以：

- **不用微信群机器人** → 忽略本节，NAS 迁移与微信无关。
- **仍要用微信群机器人** → 它现在在 Windows 上用**本机 Playwright** 直连腾讯会议，和 NAS 后端用的是**同一批 5 个账号**。两个 Playwright 同时登录同一账号，可能互相踢掉对方的网页会话。两种处理：
  1. **停用微信机器人**（最省心）：Windows 上不再跑 `meeting_bot.py`，只保留 NAS 的 H5 自助建会。
  2. **保留但改调 NAS API**（推荐若想双通道都用）：把 `meeting_bot.py` 里「本地 `tm.create_meeting_smart`」改成 **调用 NAS 的 `http://<NAS_IP>:5000/api/meeting/create`**（走 REST API，不再本机开浏览器）。这样 5 个账号的网页登录态只在 NAS 上维护一份，避免双后端互踢。

> 第 2 种是代码改动，不在本次「搬运」自动完成。需要的话单独开做。

---

## 9. 切换（Cutover）与验证清单

按顺序做完再宣布迁移完成：

- [ ] NAS 后端 `docker compose up -d` 已起，本地 `http://<NAS_IP>:5000` 能开。
- [ ] 5 个账号登录态已就绪（§6 任一种方式）。
- [ ] NAS 上 `cloudflared` 已起，且 **Windows 的 cloudflared 已停**。
- [ ] `curl https://api.rikka.com.cn/` 返回 NAS 页面内容。
- [ ] 用 `meet.rikka.com.cn` 实测**完整建会一次**：拿到会议号 + 链接 + **主持人密钥：225800**，且 `docker logs -f meeting-bot` 里有这次请求。
- [ ] 卡密扣减正常（`cards.db` 已迁移，次数正确）。
- [ ] 观察 1 天无异常（建会成功率、登录态未掉）。

> 切换期间 Windows 的 `h5/app.py` 可以直接关掉——前端流量已全部走 Tunnel 到 NAS。

---

## 10. 回滚方案

NAS 出问题想退回 Windows 原状：

1. 停 NAS 的 `cloudflared`：`docker stop cloudflared`。
2. 在 Windows 上重新启动原来的 cloudflared + `h5/app.py`。
3. 前端 `api.rikka.com.cn` 立刻指回 Windows，业务恢复。
4. **卡密不丢**：NAS 的 `deploy/data/cards.db` 是复制体，Windows 原 `h5/cards.db` 仍在；回滚后继续用 Windows 那份即可。

---

## 11. 日常运维

| 操作         | 命令                                                                                                  |
| ---------- | --------------------------------------------------------------------------------------------------- |
| 看日志        | `docker logs -f meeting-bot`                                                                        |
| 重启后端       | `cd <NAS_PROJECT_DIR>/deploy && docker compose restart`                                        |
| 升级代码       | 方式 A：`scp` 重新覆盖目录 → `docker compose build` → `docker compose up -d`；方式 B：`git pull` → build → up -d |
| 登录过期       | 重跑 §6.1，只登过期的账号即可                                                                                   |
| 重启 NAS 后自启 | `docker compose` 已配 `restart: unless-stopped`；`cloudflared` 也配了 `restart unless-stopped`            |

---

## 12. 常见坑（踩过一次就记住）

- **克隆没先 push** → NAS 拿到旧代码，缺主持人密钥/admin 405/30 秒等修复。✅ 首迁用「复制目录」或先 `git push`。
- **userid 漏了 829 前缀** → 登录命令报错或登错账号。✅ 严格用 §6 列出的 5 个完整 userid。
- **忘了搬 `cards.db`** → 所有卡密清零、已用次数归零。✅ §4 必拷。
- **Windows 和 NAS 同时跑 Tunnel** → `api.rikka.com.cn` 在两家间跳，建会时灵时不灵。✅ 先停 Windows 再起 NAS（§7）。
- **profile 跨平台不兼容** → 登录态用不了。✅ 直接走 §6.1 重新登录。
- **端口被占** → 改 `docker-compose.yml` 里 `ports` 的**左侧**（主机端口）。
- **`.env` 里密码含 `$` 等特殊字符** → 用双引号包住，如 `SECRET_KEY="ab$cd"`，否则被 shell 解析。
- **无头 Chromium 崩溃** → 镜像已带 `--no-sandbox --disable-dev-shm-usage`；仍崩就把 `shm_size` 调到 `512mb`。
