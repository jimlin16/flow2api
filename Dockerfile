FROM mcr.microsoft.com/playwright/python:v1.53.0-jammy

WORKDIR /app

# 安装 Xvfb
RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（使用清华 PyPI 镜像）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# 使用 Playwright 官方鏡像已含瀏覽器，無需重複安裝
# 僅在需要特定版本時才保留，這裡移除以加速構建
# RUN playwright install chromium

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "xvfb-run -a --server-args='-screen 0 1280x1024x24' python3 -u main.py"]
