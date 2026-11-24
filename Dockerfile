FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 토큰·승인 파일을 읽을 수 있는 그룹(mosasecret, gid 10001)과 비root 사용자(uid 10001)
RUN groupadd --gid 10001 mosasecret \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mosasaurus \
    && mkdir -p /app/data /app/run \
    && chown -R 10001:10001 /app/data

COPY src ./src
COPY config ./config
COPY migrations ./migrations

USER 10001:10001
CMD ["python", "-m", "src.main"]
