FROM node:24-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime
ENV TZ=Asia/Shanghai PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY deploy/requirements-cloud.txt deploy/requirements-cloud.txt
COPY backend/requirements-api.txt backend/requirements-api.txt
RUN pip install --no-cache-dir -r deploy/requirements-cloud.txt \
    && install -D "$(python -c 'from importlib.resources import files; print(files("tzdata.zoneinfo.Asia")/"Shanghai")')" /usr/share/zoneinfo/Asia/Shanghai \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home appuser
COPY . .
COPY --from=frontend-build /build/frontend/dist/ frontend/dist/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 CMD ["python", "deploy/healthcheck.py"]
CMD ["bash", "deploy/run_daily_shadow.sh", "ServeApi", "--host", "0.0.0.0", "--port", "8000", "--python", "python"]

FROM runtime AS local
USER root
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends iptables util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=phase10_evidence / /app/
USER 10001:10001

FROM runtime AS cloud
