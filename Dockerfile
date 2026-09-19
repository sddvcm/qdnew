FROM python:3.12-slim

WORKDIR /app

# 不生成 .pyc（容器内不需要），日志实时输出
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ⚠️ 这里的密钥只是**镜像里的占位默认值**。
# 真实密钥通过 docker-compose 的 environment 注入（读 .env）覆盖掉它。
# 不要在这里写真实密钥 —— 镜像可能被别人拿到。
ENV CHECKIN_SECRET_KEY=change-me-to-a-random-string

# onnxruntime / opencv 是预编译 wheel，正常不需要编译工具。
# 装 libglib2.0-0 是个保险：部分 opencv 版本运行时会链接到它，
# 缺了会在 import cv2 时报 "libGL.so.1: cannot open shared object file"。
# （仅当启用本地验证码识别 WITH_CAPTCHA=true 时才需要 opencv）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 依赖单独一层：代码改动时不用重装依赖（利用 Docker 层缓存）
#
# 默认只装核心依赖（requirements.txt，约 30MB）。
# 本地验证码识别（ddddocr/opencv/onnxruntime/numpy，约 400MB）是**可选**的：
#   构建时加 --build-arg WITH_CAPTCHA=true 才会装 requirements-captcha.txt。
# 不装时验证码请用云码（cloud 后端）；captcha.py 对缺失依赖有人话兜底。
ARG WITH_CAPTCHA=false
COPY requirements.txt requirements-captcha.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
       -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && if [ "$WITH_CAPTCHA" = "true" ]; then \
           pip install --no-cache-dir -r requirements-captcha.txt \
               -i https://pypi.tuna.tsinghua.edu.cn/simple; \
       fi

# 拷代码（.dockerignore 已排除 .env / data / logs / .git 等，不会进镜像）
COPY . .

# 数据目录（会被 compose 的 volume 覆盖；这里只保证容器内路径存在）
RUN mkdir -p /app/data /app/logs /app/user_plugins

EXPOSE 5800

# 健康检查兜底（compose 里也定义了，这里保证单独 docker run 也能用）
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5800/')" || exit 1

CMD ["python", "-m", "app.main"]
