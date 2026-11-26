# Phase 9B 全收益指数、跨资产 ETF 与容器验收

## ① 采集和核销

累计实际 HTTP 请求 **1**，预算上限 **140**。最近一次从已核验回执读取，新增 HTTP 为 0。最小间隔 1.05 秒。

| 标的 | 状态 | 依据 |
|---|---|---|
| H00300.CSI | blocked | H00300.CSI 20150105–20151231 OHLC/前收缺值 {'open': 244, 'high': 244, 'low': 244, 'close': 0, 'pre_close': 0} |
| 510880.SH | not_collected | — |
| 511010.SH | not_collected | — |
| 513100.SH | not_collected | — |
| 518880.SH | not_collected | — |

## ② 回放与 IS 选择

只完成可独立核验的 IS 价格指数 T0。T1/T2、X1–X4、留出窗口及安慰剂受 Stage 1 阻断。 Stage 2 HTTP 请求为 **0**。

| 模型 | 时钟 | 窗口 | 资金 | 层 | CAGR % | MaxDD % | Sharpe | Calmar | 弱市周 | 切换 |
|---|---|---|---|---|---|---|---|---|---|---|
| T0_PRICE_2PCT | ANN | IS | 450000 | 3 | 11.8892 | -26.5694 | 0.7890 | 0.4475 | 164 | 8 |
| T0_PRICE_2PCT | ANN | IS | 450000 | 4 | 12.0556 | -26.4355 | 0.7983 | 0.4560 | 164 | 8 |
| T0_PRICE_2PCT | ANN | IS | 2000000 | 3 | 11.9637 | -26.6618 | 0.7922 | 0.4487 | 164 | 8 |
| T0_PRICE_2PCT | ANN | IS | 2000000 | 4 | 12.1445 | -26.5190 | 0.8023 | 0.4580 | 164 | 8 |
| T0_PRICE_2PCT | CONT | IS | 450000 | 3 | 11.3911 | -26.5891 | 0.7682 | 0.4284 | 164 | 8 |
| T0_PRICE_2PCT | CONT | IS | 450000 | 4 | 11.5353 | -26.4329 | 0.7765 | 0.4364 | 164 | 8 |
| T0_PRICE_2PCT | CONT | IS | 2000000 | 3 | 11.3595 | -26.7198 | 0.7657 | 0.4251 | 164 | 8 |
| T0_PRICE_2PCT | CONT | IS | 2000000 | 4 | 11.5457 | -26.5808 | 0.7763 | 0.4344 | 164 | 8 |

T0 的 8 组 IS 净值与现金路径均与 Phase 9A 回放逐日逐分一致。选择锁状态为 blocked，置换 CSV 只有表头。

## ③ 本机容器

Docker 状态为 `daemon_unavailable`。可访问地址为 无。

| 探查 | 退出码 | 依据 |
|---|---|---|
| compose_version | 127 | docker_cli_not_found |
| docker_info | 127 | docker_cli_not_found |
| docker_version | 127 | docker_cli_not_found |

未执行的容器验收包括构建、健康探针、SPA 深链、治理接口、只读与 BasicAuth、非 root WAL 和并发读。

PowerShell 的本机入口为 `scripts/run_local_docker.ps1 -Action Up`，Linux/macOS 的入口为 `bash deploy/run_local_docker.sh -Action Up`。两端使用独立 `.docker_runtime/shadow/` 副本，`Down` 保留运行数据。

## ④ 测试及签发保护

全仓 Python 175 项，Phase 9A 定向 13 项，Phase 9B 定向 10 项，后端 API 4 项，Vitest 10 项。Node 对账、离线 UI、金样本对账均通过。运行的回归零失败、零跳过。

Phase 8 的 385 个保护文件、Phase 8 清单及 Phase 9A 的 5 个指定产物，合计 391 个文件全部逐字节一致。Phase 9A 源码签名另行复核。冻结核心和正式 SQLite 的实测哈希见签发清单。

