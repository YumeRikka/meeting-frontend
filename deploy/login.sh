#!/bin/bash
# 启动虚拟显示 + VNC + noVNC，供首次登录腾讯会议账号使用
set -e
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
sleep 2
x11vnc -display :99 -forever -nopw -listen 0.0.0.0 -rfbport 5900 >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo "=========================================================="
echo " ① 浏览器打开： http://<你的NAS_IP>:6080"
echo " ② 你会看到一个虚拟桌面（暂时是空的）。"
echo " ③ 在电脑上另开一个终端，对 5 个账号各执行一次："
echo "      docker exec -it meeting-bot-login \\"
echo "        python /app/python/create_via_web.py --login <userid>"
echo "    例如： --login admin1783592634"
echo " ④ 回到 noVNC 桌面，完成扫码 / 输密码登录，登录窗口可关。"
echo " ⑤ 5 个账号全部登完后，到这里按 Ctrl+C，然后："
echo "      docker compose -f docker-compose.login.yml down"
echo "      docker compose up -d"
echo "=========================================================="
wait
