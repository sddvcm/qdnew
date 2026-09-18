FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV CHECKIN_SECRET_KEY=change-me-to-a-random-string

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

RUN mkdir -p /app/data /app/logs /app/user_plugins

EXPOSE 5800

CMD ["python", "-m", "app.main"]
