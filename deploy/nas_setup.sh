#!/usr/bin/env bash
# ============================================================
# meeting-bot · NAS 部署脚本（在 NAS 上 ssh 进去后执行）
# 前置：
#   1) 本机已 `git push origin main`（本地领先 1 个提交 9978e9b）
#   2) NAS 已装 Docker（docker -v 正常）
#   3) 本机已把凭证 scp 到 <NAS_DIR>/deploy/config 与 /data（见本机侧步骤）
# ============================================================
set -euo pipefail

# ===================== ① 填写变量 =====================
NAS_DIR="/volume1/docker/meeting-bot"                 # ← 改成你 NAS 上的真实绝对路径
GIT_REPO="https://github.com/YumeRikka/meeting-frontend.git"  # ← 改成你的仓库地址（clone 用）
CF_TOKEN=""                                           # ← 从 Cloudflare 控制台复制 meeting-api 隧道的 Token

# ===================== ② 拉代码 =====================
PARENT="$(dirname "$NAS_DIR")"
mkdir -p "$PARENT"
if [ -d "$NAS_DIR/.git" ]; then
  echo "[更新] git pull -> $NAS_DIR"
  git -C "$NAS_DIR" pull
else
  echo "[克隆] git clone -> $NAS_DIR"
  git clone "$GIT_REPO" "$NAS_DIR"
fi

cd "$NAS_DIR/deploy"

# ===================== ③ 检查凭证已就位 =====================
for f in config/.env config/accounts.json data/cards.db; do
  if [ ! -f "$f" ]; then
    echo "✗ 缺少 $f"
    echo "  请先在本地 Windows 执行 scp 把它传过来（见下方『本机侧步骤·第 2 步』），再重跑本脚本。"
    exit 1
  fi
done

# ===================== ④ 构建并启动后端 =====================
# 注意：极空间若装的是老版 docker-compose（v1），把下面 `docker compose` 改成 `docker-compose`
docker compose build
docker compose up -d

echo "等待后端启动..."
sleep 6
docker logs --tail 20 meeting-bot
echo "本地验证： curl -s http://localhost:5000/ | head -n 5"

# ===================== ⑤ 5 个账号登录（需人眼扫码，脚本只起 noVNC）=====================
echo "------------------------------------------------------------"
echo "现在登录 5 个腾讯会议账号（登录态持久化到 deploy/data/profiles，重启不丢）："
echo "  1) 起虚拟桌面： docker compose -f docker-compose.login.yml up -d"
echo "  2) 浏览器开 http://<NAS_IP>:6080"
echo "  3) 另开终端，对 5 个账号各执行一次："
echo "       docker exec -it meeting-bot-login python /app/python/create_via_web.py --login admin1783592634"
echo "       docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280568"
echo "       docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280570"
echo "       docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295133"
echo "       docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295134"
echo "  4) 在 noVNC 桌面里完成扫码 / 输密码登录（登录窗口可关）"
echo "  5) 5 个全登完后："
echo "       docker compose -f docker-compose.login.yml down"
echo "       docker compose restart"
echo "------------------------------------------------------------"

# ===================== ⑥ 起 Cloudflare 隧道（复用 meeting-api 命名隧道）=====================
if [ -z "$CF_TOKEN" ]; then
  echo "⚠ CF_TOKEN 为空，请填好顶部变量后重跑本步；或手动执行："
  echo "  docker run -d --name cloudflared --restart unless-stopped --network host \\"
  echo "    cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <你的TOKEN>"
else
  docker run -d --name cloudflared --restart unless-stopped --network host \
    cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$CF_TOKEN"
  echo "隧道已起。验证： curl -s https://api.rikka.com.cn/ | head -n 5"
fi

echo "✅ 部署脚本执行完毕。"
echo "⚠ 最后一步：在本机 Windows 关掉 run_tunnel.bat 那个窗口（停掉本机 cloudflared），"
echo "   避免两台机器抢 api.rikka.com.cn 这个域名。"
