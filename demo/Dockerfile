FROM python:3.11-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

CMD ["python", "run_pipeline.py"]
