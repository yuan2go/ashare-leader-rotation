# Phase 11 量化工作台验收

本机工作台已在 http://127.0.0.1:8000/ 以 Interactive 模式健康运行。影子账本最新交易日为 2026-11-26，API 仍以 SQLite 只读连接读取影子库。

## 交付内容

- 股票池矩阵提供四种预设、行业与代码筛选、排序及影子持仓标记。真容器返回 280 只成分股。
- 全局 K 线抽屉可从股票池、ETF 雷达、持仓成交和开盘指令打开。真容器对 600015.SH 返回 240 根前复权日线，按回看日确定复权基准。
- ETF 雷达返回 8 只核销 ETF，单列 513100.SH，并显示各标的实际行情日。持仓及 ValueDiv 候选相关性矩阵为 20 × 20，持仓范围为 DEF50、E4M、V5、C3。
- 实盘钱包提供净值、现金、逐股差异、年化跟踪误差及录入和删除。数据库位于隔离运行目录的 wallet 子目录。只读模式下钱包与运维写请求均返回 403。交互模式的临时快照写入和删除均返回 200，且运行影子库哈希保持一致。
- 研究档案新增 Phase 9–10 页签，读取 Phase 9A 真实摩擦消融、Phase 9B 跨资产、Phase 10 CH-4 因子法庭、组合与相关性结果。VPB 裁决为 REJECT_ARCHIVE_ONLY。
- 健康接口新增 api_read_only，运维和钱包据此显示提示并控制写操作。原 21 个端点保有历史请求与响应字段兼容性。
- 新接口包括 GET /api/v1/market/universe、GET /api/v1/market/kline/{code}、GET /api/v1/market/radar、GET /api/v1/market/correlation、GET /api/v1/wallet/summary、POST /api/v1/wallet/snapshot、DELETE /api/v1/wallet/snapshot/{day} 及 GET /api/v1/research/multialpha。

## 验收

| 项目 | 结果 |
| --- | --- |
| 全仓 Python 回归 | 187 项通过 |
| 独立既有后端 API 与 Phase 11 API | 各 4、4 项通过 |
| Vitest | 14 项通过 |
| 完整性、OpenAPI、契约类型、TypeScript、Vite 构建 | 全部退出 0 |
| 多阶段 Docker 构建与只读冒烟 | 各退出 0，包含 Basic Auth、非 root 同卷 WAL、并发读及旧版 SPA 与 API |
| Docker 交互模式冒烟 | 退出 0，影子库保持只读，钱包录入和删除通过 |
| 本机浏览器 | 验证真实 K 线抽屉、雷达热力图、钱包路由与研究瀑布图 |

回归无失败、无跳过。测试与容器冒烟的行情 HTTP 请求为 0。校验的 391 项受保护文件和 11 项 Phase 9A 源码前后 SHA-256 完全一致。签发版 OpenAPI 和 Markdown 契约保留原字节，本期分别生成 backend/openapi_phase11.json 与 docs/api_contract_phase11.md，前端类型已同步。

- engine.py `090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80`
- offline/engine.js `26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945`

## 本机运行命令

```powershell
.\scripts\run_local_docker.ps1 -Action Start -Mode ReadOnly
.\scripts\run_local_docker.ps1 -Action Down
```
