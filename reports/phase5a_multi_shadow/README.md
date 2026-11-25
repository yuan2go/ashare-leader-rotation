# Phase 5A 每日影子账本

五条路径分别以 ¥450,000 和 ¥2,000,000 从全现金起跑。首个交易日为 2026-11-23。DEF50 与 E4M 使用签发的 2026-11-20 防御信号，EW_PROP 在首日开盘建仓，指数基准以首日开盘为起点。C3 的每个账户输出带 `exploratory_observer_only` 水印。

## 日常运行

在仓库根目录执行。默认加载本地 Python 3.11，`-Python` 可指定另外的 3.11 解释器。Tushare 配置读取被 Git 忽略的仓库 `.env` 或同名环境变量。

```powershell
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action Healthcheck
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action DailyAll
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action Collect -Date 2026-11-23
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action Ingest
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action Status
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action VerifyOffline
```

建议在 Windows Task Scheduler 为 `powershell.exe` 配置每日 **18:30（中国标准时间，UTC+08:00）** 启动，参数为 `-NoProfile -File "D:\projects\ashare-leader-rotation\scripts\run_daily_shadow.ps1" -Action DailyAll`。开始位置设为仓库根目录，已有实例运行时跳过新实例。休市日由沪深交易日历识别。若当地 Windows 时区不同，把触发器转换到对应本地时间。任务以有仓库和 Tushare 配置读取权限的用户运行。

`DailyAll` 采集当日行情、尝试在 `factor_staging/` 固化当日估值、按日期入账完整日包并重建输出。查询 `last_collection_status.json` 可区分采集成功、休市及 `pending_upstream_publication`，逐日记录在 `collection_status/`。如遇上游未齐，稍晚重试该日。先补齐缺失日，再推进待入账的后续日包。重复提交相同日包自动跳过，修改已提交字节或直接跳过缺失交易日会失败。

`live_timely_verified` 以 T-1 估值响应的本地收包和 HTTP `Date` 为准。晚于决策截止的回执归入 `post_hoc_catchup`。原始 HTTP 回包与回执在 `raw/`，估值字段保留供应商空值。v1 导出使用 `-ExportPhase3dV1 <目录>`，并额外检查旧协议各节的可用时间。来自晚间轮询的开盘节通常缺少 v1 所需的开盘收包证据。

## 结果与验证

`shadow_v5a.sqlite3` 是权威账簿。可读结果包含两档资金的逐日权益、袖子盈亏、持仓、成交、公司行动与严格／补采会话数。次日指令单仅列 ¥450,000 主档，按当日收盘代理价估算。查看下一个预定信号及 M200 的周频交易状态，以实际次日开盘重新检查报价及门禁。

离线验收仅访问签发快照，在临时库重放后形成 `offline_acceptance.json`。影子账本复用 Phase 4B／4C 撮合内核，以逐日入包重建与整窗批处理对账。

[估值桥接核验](phase5a_bridge_audit.md) · [签发哈希与验证记录](phase5a_manifest.json)
