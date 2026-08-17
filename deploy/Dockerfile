# 会议卡密后端 · Docker 镜像（x86_64 / amd64）
# 基于官方 python:slim，自行安装 Playwright Chromium 与首次登录用的虚拟桌面(VNC)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# 系统依赖：Playwright Chromium 运行库 + 首次登录用的虚拟显示/VNC(noVNC)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    xvfb x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（不含 wxauto4 —— 那是 Windows 微信机器人用的，后端不需要）
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium 浏览器及系统依赖
RUN playwright install --with-deps chromium

# 拷贝业务代码
COPY python/ ./python/
COPY h5/ ./h5/
COPY deploy/login.sh ./login.sh
RUN chmod +x ./login.sh

# 让 h5/ 与 python/ 都可被 import（app.py 在 h5/，cards 在 h5/，tencent_meeting 在 python/）
ENV PYTHONPATH=/app/h5:/app/python
ENV DISPLAY=:99

EXPOSE 5000 6080
CMD ["python", "/app/h5/app.py"]
