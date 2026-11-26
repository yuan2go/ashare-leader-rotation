"""Export the checked-in OpenAPI 3.1 and the Phase 7B contract from FastAPI."""
import argparse
import json
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import ROOT
from backend.app.main import app

INTRO = """# Phase 7A API v1 接口契约

服务默认监听 `http://127.0.0.1:8000`，前缀为 `/api/v1`。Swagger UI 位于 `/docs`，同一份规范在线地址为 `/openapi.json`。本文件和 `backend/openapi.json` 由 `python -m backend.export_openapi` 生成。添加 `--check` 可检查签入文件是否与当前服务一致。

## 运行与 TypeScript 接入

```powershell
$Py = 'data/fetch_cache/runtimes/batch1-py311/Scripts/python.exe'
& $Py -m pip install -r backend/requirements-api.txt
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action ServeApi
```

在已安装 `openapi-typescript` 的 Phase 7B 项目中，使用 `openapi-typescript ../backend/openapi.json -o src/api/schema.ts` 生成类型。以 `paths` 的响应及参数类型绑定请求，以 `components.schemas` 复用实体类型。默认 CORS 允许 `http://localhost:5173` 和 `http://127.0.0.1:5173`。

## 接口

| 方法 | 路径 | operationId | 用途 |
| --- | --- | --- | --- |
"""

DETAILS = """
## 字段与取值

- `_cents` 是人民币整数分，`_rmb` 是人民币元。`_pct` 是以 100 为满值的百分数，`_pp` 是百分点。净值、收益、最大回撤、超额和整手股数由服务端提供。置换摘要的 `actual`、`inverse` 和 `permutation_median` 由 `metric_unit` 区分比例与百分数。
- `account`、`role_tag` 和 `risk_label` 由各条路径返回。用 `account` 与 `capital_rmb` 联合标识账户。基准的股数可为小数，股票及 ETF 代理指令的数量为整数。
- 影子明细可用 `account`、`capital_rmb`、`start`、`end` 筛选。日期为 ISO 交易日，首尾均包含。持仓默认取最后入账日。成交、持仓与公司行动返回 `items`、`offset`、`limit`、`has_more`。净值日期筛选保留账户起始基准与历史高点。
- `live_timely_verified` 是严格时点会话。历史补采日保持 `post_hoc_catchup` 与原收包时间。指令单按主档 ¥450,000 生成，`as_of` 标记生成所依据的账簿交易日，`opening_day` 为试算开盘日。
- `m200.state` 是最后一个周信号采用的状态，`m200.daily_state` 为当日迟滞状态。`vol_monitor.applied_attack_cap_pct` 为已入账的 V5 目标，`effective_attack_cap_pct` 包括最新收盘已确定的下一开盘目标。首次月末选出目标前，`target_etf_code` 与死区距离为空。`reference_etf_code` 可提供上一签发月末选中的 ETF 作为波动率参考。
- 签发归档接口使用原归档的 `source`、`pool`、`key` 和窗口。组合摘要默认 W1／2bp／¥450,000，逐年收益与回撤按现有档案返回。Phase 4B 的 C3 逐年筛选返回空数组。未恢复回撤使用 `recovered=false` 与 `recovery_date=null`。Phase 6A 置换归档含 E4M、V4、V6，可用 `include_draws=true` 读取抽样行。
- JSON 错误格式为 `{"code":"...","message":"..."}`。422 表示参数校验失败，409 表示运维冲突，503 表示本地输入或完整性校验不可用。

## 本地运维

`POST /api/v1/ops/jobs` 接收 `X-Shadow-Ops: 1` 和 JSON 请求。任务返回 202，使用 `id` 轮询状态及日志。一次执行一个任务，状态和日志跨 API 进程重启保存在运维目录。

| Action | 数据边界 |
| --- | --- |
| `Ingest` | 离线验证日包、更新 Phase 7A 账簿和导出 |
| `VerifyOffline` | 在临时库重放 73 日并更新 Phase 7A 验收文件 |
| `Collect` | 需要 `allow_market_http=true`，可指定 `day` |
| `DailyAll` | 需要 `allow_market_http=true`，先采集，再按顺序入账 |

采集任务结束后读取 `/ops/collection-status` 判断上游是否完整发布。该接口展示原记录的请求数、更新时间及来源版本。Phase 5A 签发档案通过只读接口保留。脚本 `-Profile v5a` 兼容五线流程，续跑写入 Phase 7A 的 `legacy_v5a/`。

## 环境变量

| 变量 | 默认值或作用 |
| --- | --- |
| `PHASE7A_DIR` | `reports/phase7a_service_shadow` |
| `PHASE7A_DB` | Phase 7A 目录下的 `shadow_v7a.sqlite3` |
| `PHASE7A_REPORTS` | 仓库 `reports`，用于签发研究及历史采集状态 |
| `PHASE7A_ENGINE_LOCK` | 仓库 `engine_integrity.json` |
| `PHASE7A_MANIFEST` | Phase 7A 目录下的 `phase7a_manifest.json` |
| `PHASE7A_OPS_LOCK` | Phase 7A 目录下的 `ops/execution.lock` |
| `PHASE7A_FRONTEND_DIST` | 仓库 `frontend/dist`，存在 `index.html` 时托管 |
| `PHASE7A_CORS_ORIGINS` | 逗号分隔的前端来源 |
"""


def contents():
    spec = app.openapi()
    if not spec["openapi"].startswith("3.1."):
        raise RuntimeError("Phase 7B requires OpenAPI 3.1")
    table = []
    for path, methods in spec["paths"].items():
        for method, metadata in methods.items():
            table.append(f"| `{method.upper()}` | `{path}` | `{metadata['operationId']}` | {metadata['summary']} |")
    return {
        ROOT/"backend/openapi.json":json.dumps(spec,ensure_ascii=False,indent=2)+"\n",
        ROOT/"docs/api_contract_v1.md":INTRO+"\n".join(table)+"\n"+DETAILS,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check",action="store_true")
    args = parser.parse_args()
    for path, data in contents().items():
        if args.check:
            if not path.is_file() or path.read_bytes() != data.encode("utf-8"):
                parser.exit(1,f"Stale API contract: {path.relative_to(ROOT)}\n")
        else:
            path.write_text(data,encoding="utf-8",newline="\n")


if __name__=="__main__":
    main()
