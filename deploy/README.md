# Phase 8 云端部署

驾驶舱常驻服务只监听宿主机回环地址，写入任务由独立 worker 执行。签发原库保留在 `reports/`，运行账本放在独立目录。云端上线需要同一份 Phase 7C 源码和签发档案。

## 恢复签发数据和运行账本

在签发工作区使用 Python 3.11 导出。迁移累积账本前暂停采集和入账，避免数据库快照与日包、票单处于不同日期。若本机已将运行账本迁到独立目录，导出前将 `PHASE7A_DIR` 指向该目录。

```bash
python3.11 scripts/bundle_deploy_data.py --export /tmp/ashare-signed-data.tar.gz
python3.11 scripts/bundle_deploy_data.py --export /tmp/ashare-with-runtime.tar.gz --include-runtime
sha256sum /tmp/ashare-with-runtime.tar.gz
```

基础包包含 Phase 7C 清单中未入 Git 的 222 个文件，包括股票 PIT、Phase 5A 日包及回执、Phase 6A 探针回执和两份校验日志。运行包额外携带 SQLite 快照、日包、因子阶段文件、原始回执、采集状态和现有票单。运行副本导入到 `deploy-runtime/shadow`。

通过可信渠道传输归档和外部 SHA-256，并在目的端比较。部署源码必须带齐 `reports/phase7c_hardening/phase7c_manifest.json` 以及同目录的 V5 置换 CSV 与裁决 JSON。若 Git 还未收录这些 Phase 7C 文件，从同一签发工作区随源码传入。M200 分层归档位于 `reports/phase7c_m200_stratified/`。

```bash
sha256sum /tmp/ashare-with-runtime.tar.gz
umask 022
python3.11 scripts/bundle_deploy_data.py --import /tmp/ashare-with-runtime.tar.gz --target .
python3.11 scripts/bundle_deploy_data.py --verify --target .
```

导入会先校验整个归档并对照目的端的 Phase 7C 签发清单。现有相同字节可以重复导入。若运行目录已有不同的账本，先停止 worker，再选择一个新的目标目录完成迁移。保留原账本作为恢复点。

## 容器与 HTTPS

在新部署中从 `.env.example` 创建本机 `.env` 并设为仅部署用户可读。将 `PHASE7A_ALLOWED_HOSTS` 设为实际域名或 IP，并保留 `127.0.0.1` 供探活。需要应用层认证时同时设置 `PHASE7A_BASIC_AUTH_USER` 和 `PHASE7A_BASIC_AUTH_PASS`。在公网接入时使用 HTTPS，并保持默认 API 只读。Compose 只给 worker 注入行情和通知通道环境变量。

```bash
sudo chown -R 10001:10001 deploy-runtime/shadow
docker compose config --quiet
docker compose build cockpit
docker compose run --rm --no-deps worker Healthcheck
docker compose up -d cockpit
curl --fail http://127.0.0.1:8000/api/v1/health
```

证书在 Caddy 或 Nginx 层终结。采用 [Caddy 模板](Caddyfile.example) 时，替换域名并按部署环境加载。Nginx 对应配置应主动覆盖 `Host`、`X-Forwarded-Host` 和 `X-Forwarded-Proto`，并指向宿主机 `127.0.0.1:8000`。确实只有受信反代能访问后端时，可设 `PHASE7A_TRUST_PROXY_HEADERS=1`，让运维接口按外部 HTTPS 源核验请求。若需 Uvicorn 记录真实客户端 IP，另设置 `PHASE7A_UVICORN_FORWARDED_ALLOW_IPS` 为后端看到的实际代理 IP。Tailscale 接入可以使用其 HTTPS Serve 并保留同样的回环端口绑定。

在外部运行目录可显式设置 `PHASE7A_SQLITE_WAL=1`。容器共享同一份本机文件系统上的运行目录，SQLite 读连接使用 `mode=ro`，进程仍需要访问 WAL 及共享内存文件。保留已签发的仓库内 SQLite 原库。更换运行账本前停止 cockpit 和 worker。

## 日常运维

```bash
docker compose run --rm --no-deps worker Status
docker compose run --rm --no-deps worker VerifyOffline
docker compose run --rm --no-deps worker DailyAll --date YYYY-MM-DD
docker compose run --rm --no-deps worker DailyAll --notify-on-pending
```

`DailyAll` 先采集再入账，`Collect` 只采集，`Ingest` 从现有日包入账。计划任务按上海时区运行，日期参数用于明确指定补采日。采集退出码 `10` 表示上游未发布。通知脚本用最终业务退出码判断告警，通知送达错误写入标准错误，原业务退出码仍传回计划任务。

配置 `SHADOW_NOTIFY_WEBHOOK_URL` 可使用飞书或通用 JSON Webhook。SMTP 需要主机、发件人和收件人，465 使用 TLS，其他端口使用 STARTTLS。填写 `SHADOW_NOTIFY_DRY_RUN=1` 可只打印决策，或用以下命令独立预览。预览命令只读运行状态和票单。

```bash
python3.11 scripts/notify_shadow_event.py --status-dir deploy-runtime/shadow \
  --exit-code 0 --action DailyAll --date YYYY-MM-DD --dry-run
```

[systemd 模板](systemd/)采用工作日 19:00、20:30 和 22:00 三个时槽，只有 22:00 报告持续待发布。将模板复制到 `/etc/systemd/system/`，修改其中的工作目录和 Docker 路径，执行 `systemctl daemon-reload`，再启用 `ashare-shadow-retry.timer` 和 `ashare-shadow-final.timer`。用 `systemctl list-timers` 核对上海时区下的触发时间，通过 `journalctl -u 'ashare-shadow*.service'` 查看执行情况。

不用容器时，导入运行包后，从环境或进程管理器设置部署变量及仓库外的 `PHASE7A_DIR`。使用已有依赖运行 `bash deploy/run_daily_shadow.sh BuildFrontend`，再用 `bash deploy/run_daily_shadow.sh ServeApi` 启动。其余运维命令同名。独立安装 Python 依赖使用 `deploy/requirements-cloud.txt`。
