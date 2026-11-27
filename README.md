# ashare-leader-rotation

## 当前工作区与研究边界

v4.0 导入基线含 16 股与 7 只 ETF。当前签发工作区含 50 股，49 股敏感性对照剔除 300308，回放初始资金为 450,000 元。
两份冻结引擎为 `engine.py` 和 `offline/engine.js`，由 `engine_integrity.json` 固定身份。三份输入协议锁覆盖 `policy.json`、`inputs.py` 和 `offline/input.js`。`core/` 共享研究层复用冻结成交原语，策略实验在研究层运行。
`core/calendar.py` 分别定义交易月末、已完整自然月的最后周信号与锚定双周信号。`core/metrics.py` 的 `period_oneway_turnover_pct` 为窗口累计成交额除以两倍日均权益的百分数，`ann_oneway_turnover_pct_true` 再乘 242 / 窗口交易日数。历史 P1 列 `ann_oneway_turnover_pct` 是窗口累计值。Calmar 用自然日年化收益率除以最大回撤绝对值，`net_over_dd` 用窗口净收益。恢复日可选严格创新高或回到前高。
50/50 BASE 的趋势袖子有 2 个槽，防御袖子有 3 个。防御在完整自然月的最后一个已公告周信号日换仓。BIWEEKLY 是趋势袖子对照。W_Bear 按 policy 的 2021-03-01 至 2023-11-19，跨实验比较须核对首末交易日与实际会话数。
P1 验收表含 11 个容差锚点（收益与回撤各容许 0.05 个百分点）；44 组影子账本逐日逐笔精确对账。离线 HTML 属于旧研究的签发身份，复核历史实验时保留原快照。
无参 `node tests/parity.cjs` 以 --parity-only 启动 Python 合成回归。指定包文件时，JS 读取该包并核对准备结果、信号、逐日账本和成交。
公开 CI 在 Python 3.11 / Node 24 上运行冻结核验、合成对账、无网采集、API 契约和前端构建。签发回归使用本仓库 Actions 的 `stage2-inputs` 归档，配置 `STAGE2_FIXTURE_RUN_ID` 和包 `stage2-inputs.zip` 的 `STAGE2_FIXTURE_SHA256` 后执行全量发现和签发账本 API 测试，也可手动选择 `signed_replay`。包内使用仓库相对路径，需涵盖 Restore signed test inputs 声明的文件和 Phase 7C 保护清单中的未入 Git 资产。签发回归启用时，缺包、缺文件或摘要不符均使验收失败。
例行 manifest 只哈希实际消费的文件，完整缓存取证另用 `python -B -X utf8 scripts/cache_forensics.py --full-cache`。历史 600428 插件状态见 `archive/legacy_research/README.md`。



A 股跨行业龙头周频趋势轮动研究台 v4.0，包含独立的宽基 ETF 资金雷达。通过可追溯输入、固定规则和 Python / 离线 JavaScript 对账，研究信号如何转化为实际可执行的扣费账户表现。

**v4.0 联网采集覆盖固定池全部上市日。差异化权益支持公告折算校验，正式回放状态见 [最新数据验收](results/rotation_v4_0_validation.md)。**

**v4.1 离线前端**沿用文件名 `rotation_quant_lab_v4_0_offline.html`。五个页面共用冻结引擎，股票矩阵按 `universe.csv` 扩展。宏观展示桶用于筛选和配色，跨桶策略按配置中的原桶执行。[参考原型](docs/ui/rotation_quant_lab_v4_1_ui_prototype.html)中的数据为演示数据。

## 原包导入基线

2026-11-23 从 `rotation_v4_0_python_backend.zip` 导入，原策略、配置与研究结果保留；两份冻结引擎的字节哈希与初始化蓝图一致。详见 [初始化与验证记录](docs/INITIALIZATION.md)。

| 项目 | 当前状态 |
| --- | --- |
| 股票池 / ETF | 导入时 16 股、7 个股票桶。当前工作区 50 股、7 只宽基 ETF |
| 起始研究资金 | ¥450,000，默认研究 Top 1 / Top 2 |
| 开发期 | 配置起日 2015-01-01，首个交易日 2015-01-05；冻结至 2026-11-20，2,887 个交易日 |
| 前缀因果截断日 | 2023-01-20 |
| 股票数据 | 原包已有牧原 2,887 行；其他 15 只合计缺 40,693 行；stock_issues = 15 |
| 沪深 300 | 原包输入校验缺口为 0 |
| ETF 数据 | 现有 1 条份额记录，冻结日有效记录为 0；etf_issues = 0 只表示现有记录未触发校验错误 |
| 正式全池回放 | production_replay = false |
| 已验证范围 | 冻结字节、干净环境工程测试、合成双端/前缀/UI验证；本次另重跑原生牧原 T1 / 沪深 300 独立对照 |

上表记录导入时的状态。`results/` 记录最近一次本地验收。原包结果可以从仓库历史中查看，本地采集前的底稿和结果也保存在 `data/fetch_cache/baseline-2026-11-23/`。新克隆时可用的行情取决于本地缓存和数据服务，验收须核对同一次运行的输入哈希。

## 策略与实现范围

1. **趋势资格**：252 个可交易日预热，日线 MA20 / MA60 与严格完整自然周的 20 周均线共同决定资格。周中退出信号依照原引擎在后续可成交开盘执行。
2. **横截面排名**：20D 与 60D 对数价格回归的年化斜率乘拟合优度，等权合成趋势质量分。规则和参数冻结，不做事后寻优。
3. **周频轮动**：R1（Top 1，前 3 缓冲）、R2（Top 2，前 5 缓冲）、R2_Bucket（Top 2 跨桶）。保留整手、历史印花税切换、佣金、停牌/涨跌停顺延、滑点及延迟压力场景。
4. **独立 ETF 观测**：按实际可得的日份额和披露时点计算覆盖及资金诊断，不作为股票回放必须全部满足的前置条件，也不据此识别资金账户身份。

本仓库牧原 T1 是 v4.0 纯价对照，与外部 v3.1 / A00 实盘账本独立。当前固定池是事后选择的现存龙头池，历史回测仍有选择与幸存者偏差。费用与公司行为处理范围见 [原包数据与执行说明](docs/archive/v4.0-source-README.md)；认购型配股、退市现金清算等未支持事件不能通过放宽校验纳入。

## 本地开始

原程序要求 Python 3.10+。本仓库 CI 使用 **Python 3.11、Node.js 24**，同时验证 Windows / Ubuntu。Python 与 Node 都必须能从命令行运行；离线页面与工程测试无 npm 依赖。

真实全池双端对账以同一套 CI 运行时复现。Python 3.12 改用不同的浮点求和算法，在本池真实行情的均线临界值上会改变严格大于判断。使用 Python 3.11 可以保留冻结引擎及原对账断言。

### Windows PowerShell

安装 Python 3.11、Node.js 24 和 Git 后执行。以下使用虚拟环境解释器的完整相对路径，不要求修改 PowerShell 执行策略：

```powershell
git clone https://github.com/yuan2go/ashare-leader-rotation.git
cd ashare-leader-rotation
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
py -3.11 -m venv .venv

.\.venv\Scripts\python.exe scripts/check_integrity.py
.\.venv\Scripts\python.exe scripts/prepare_workspace.py
.\.venv\Scripts\python.exe tests/test_engine.py
.\.venv\Scripts\python.exe tests/test_fetch.py
```

`prepare_workspace.py` 仅为不存在的 `data/stocks.csv`、`data/etf.csv` 建立正确表头，并从当前源码构建 `rotation_quant_lab_v4_0_offline.html`。已有数据不覆盖，正式审计和报告不改写。新克隆必须先执行这一步，因为原采集入口也会先读取这两张 CSV。

### macOS / Linux

```bash
git clone https://github.com/yuan2go/ashare-leader-rotation.git
cd ashare-leader-rotation
python3 -m venv .venv
source .venv/bin/activate
export PYTHONUTF8=1
python scripts/check_integrity.py
python scripts/prepare_workspace.py
python tests/test_engine.py
python tests/test_fetch.py
```

`test_engine.py` 已串联 Node parity 与 UI，涵盖合成成交、逐分账本、前缀因果和 50 标的配置测试。`test_fetch.py` 使用无网样例校验采集适配器。它们只需 Python / Node 标准库，因此 CI 不安装行情 SDK，不访问行情服务。

### 个股专项研究

已有通过验收的开发期行情时，可刷新中远海特的两项独立实验。

```powershell
python research/exp_600428_earnings.py
python research/exp_600428_flow_reversion.py
python run.py --confirm-seeds
```

两项研究的逐笔结果、样本口径和来源保存在 [600428 专项结果](results/research/600428.json)，原始观测留在 `data/research/`。财报采用上交所披露索引时间，09:25 前可用当日开盘。大单采用东方财富历史日终口径，首次披露时间未独立核验。追加日终观测用 `--through YYYY-MM-DD`，已存观测保留首次采集值，冻结期研究统计始终截止 2026-11-20。两项脚本均支持 `--offline` 复算现有证据。

构建器将 `results/research/<六位代码>.json` 中版本为 1、代码吻合且 `asof` 等于冻结日、且声明的 input_sha256 与本批次输入一致的插件注入 `RAW.research`。插件的 `experiments` 按 `earnings_event_study` / `price_down_flow_in` 协议渲染。资金、轮动选股与撮合读取原输入表和冻结规则。

### Phase 7A 影子账本 API

本地 Python 3.11 安装 `backend/requirements-api.txt` 后，运行 `powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action ServeApi`。服务默认监听 `127.0.0.1:8000`，含六线两档资金的只读账本、收盘风控和次日试算、签发研究及受控运维。[接口契约](docs/api_contract_v1.md)与 [OpenAPI 3.1](backend/openapi.json)供 Phase 7B 接入。

`run_daily_shadow.ps1` 默认使用 `-Profile v7a`。离线入账用 `-Action Ingest`，73 日历史验收用 `-Action VerifyOffline`。Phase 5A 五线续跑使用 `-Profile v5a`，签发归档作为只读输入。

使用已有前端依赖时，先运行 `run_daily_shadow.ps1 -Action BuildFrontend`，再用 `-Action ServeApi` 从同一端口访问页面与 API。设置 `PHASE7A_API_READ_ONLY=1` 可禁用运维任务提交。驾驶舱默认聚焦 E4M，V5 治理标签取自 [Phase 7C 置换补验](reports/phase7c_hardening/phase7c_report.md)。

Linux 和容器部署见 [Phase 8 云端部署手册](deploy/README.md)。部署前先恢复离线签发资产，并把运行账本放在独立可写目录。

### 本机工作台自动更新

Docker 交互模式启动后，工作台立即检查一次新日线，此后每 10 分钟检查一次。工作台停止时，更新循环一并停止。

```powershell
.\scripts\run_local_docker.ps1 -Action Up -Mode Interactive
Get-Content .docker_runtime/shadow/ops/docker_refresh_status.json
```

程序按北京时间及账本收录的交易日历检查已收盘的日线。发现合格日包后，先在 Linux 临时目录离线核验和计算，再提交到独立运行账本。已打开的页面会定期检查账本日期并自动刷新。默认只读模式关闭自动更新和行情外连。最新一次检查保存在 `docker_refresh_status.json`，新交易日的策略和基准统计保存在同目录的 `docker_refresh_analysis.json`。

### 联网补齐真实数据与回放

准备文件后，安装原包声明的采集依赖，再分别检查采集与正式回放的退出状态。以下使用已激活环境中的 `python`；Windows 未激活时用 `.\.venv\Scripts\python.exe` 替换：

```bash
python -m pip install -r requirements.txt
python fetch_v4_data.py
python run.py --confirm-seeds
```

采集器优先复用哈希匹配的已验收快照。新股票的 Tushare Pro 采集路径在三股校准和正式来源门禁通过后启用。主源传输失败时，采集器可回退 BaoStock。指数和 ETF 仍使用原有数据源。`--stocks-only` 可复核股票和权益并保留现有 ETF 表。差异化分派可在 `data/verified_actions_override.csv` 录入公告折算值。两端以该底稿逐条匹配折算字段，并按原 ¥0.015 容差验算参考价。账本仍按普通股东实际权益入账。

交易所历史日规模表标记 `exchange_t1_convention`，统计日之后的首个真实交易日为逻辑可见日，源回执保留实际下载时间。`verified_live` 要求可核验的北京时间发布时刻，15:00 后披露从后续交易日生效。历史数据的 T+1 是研究假设。冻结日统计的份额要到下一交易日才进入雷达。停牌无价的日期保留原始份额回执，联合计算使用覆盖整个窗口的固定成员。

也可先从仓库保留的核验底稿重建当前部分数据，无需行情 SDK 或联网：

```bash
python fetch_v4_data.py --offline
python run.py --confirm-seeds
```

没有本地缓存的新克隆仍会报告股票缺口。有联网底稿时，正式验收继续检查公告、参考价和两端因果回放。普通 `python run.py` 可在数据不足时构建诊断页并退出 0，冻结文件或采集回执校验失败会以非零状态退出。正式验收使用 `--confirm-seeds`，分别检查 `approved_policy`、`explicit_confirm` 和 `production_replay`。

正式回放的判断需同时满足：命令实际成功、`results/data_audit.json` 中 `stock_issues == 0`、`production_replay == true`，且对应真实数据前缀/双端对账通过。ETF 另看有效时点覆盖，不能只看 `etf_issues`。

生成后双击 `rotation_quant_lab_v4_0_offline.html` 查看。主结果入口：

- `results/rotation_v4_0_validation.md`：数据覆盖、回放与诊断说明。
- `results/data_audit.json`：源文件哈希、错误、覆盖、正式回放与独立对照状态。
- `results/rotation_research.json`：本地生成的详细结果，不提交 Git。

## 目录与数据管理

| 位置 | 用途 |
| --- | --- |
| AGENTS.md | 全部 AI 代理共用的工程与量化规范 |
| CLAUDE.md、.cursor/rules/ | 轻量引用统一规范 |
| engine.py、offline/engine.js | 冻结的 Python / JavaScript 策略与撮合引擎 |
| engine_integrity.json、scripts/check_integrity.py | 原锁文件与固定的批准哈希核验；不允许静默更换基线 |
| inputs.py、analytics.py、phase2.py | 输入验证、统计与 Phase 2 诊断 |
| run.py、fetch_v4_data.py | 原生回放/页面构建与行情采集入口 |
| universe.csv、funds.csv、policy.json | 股票池、基金池和冻结研究配置 |
| data/verified/、data/calendar.csv 等 | 保留的核验底稿、日历、证券信息、指数和权益事件 |
| offline/、tests/ | 离线研究台源码与既有验收测试 |
| .github/workflows/ci.yml | Windows / Ubuntu 工程完整性与双端一致性验证 |
| docs/archive/ | 原包 README 和全部导入文件的 SHA-256 清单 |

`.gitignore` 排除可重拉的 `data/stocks.csv`、`data/etf.csv`、`data/fetch_cache/`、本地采集日志/回执以及大体积构建产物；人工核验补丁和历史研究证据保留。发布新的研究结果时，应保留其数据身份和必要采集回执，不能只提交一个缺乏来源的收益表。

`.gitattributes` 固定普通文本为 LF，并使冻结引擎、CSV 与原始证据按原字节检出，避免 Windows 自动换行转换导致冻结哈希变化。

## 下一阶段

阶段 3 按八个宏观桶分批筛查。首批已将前四桶的 19 只候选加入正式名单。第二批按独立的八桶配置筛查，名单以 `universe.csv` 为准。原策略桶沿用其 `bucket` 列。

在 Python 3.11 下运行 `python research/screen_50_candidates.py` 生成第二批体检。加 `--batch1` 可复算[首批候选体检](results/research/universe_50_screening.md)。首批扩容保留[原 16 股名单](data/universe_seed16_backup.csv)和[原池与扩容池对照](results/research/batch1_pool_comparison.md)。

第二批使用 Tushare Pro 未复权报价与巨潮公告。将 `TUSHARE_HTTP_URL` 与 `TUSHARE_TOKEN` 设在本地 `.env` 中，或通过环境变量传入。`python research/verify_tushare_baseline.py` 输出[三股口径校准](results/research/tushare_baseline_check.md)。两端输入校验器接受指定接口和证券代码组成的 `tushare_pro:` 脱敏标识。正式池以当前 `universe.csv` 与匹配的验收审计为准。

已有官方历史档案可使用 `data/verified/stocks.csv`、`instruments.csv` 和 `actions.csv`，并用同目录的 `screening_sessions.csv`（`code,date,isST,amount,source`）及 `screening_action_coverage.csv`（`code,start,end,source`）提供冻结日风险状态、成交额和权益档案覆盖证明。正式 50 股双端对账通过后，`python research/exp_50_pool_v5_precheck.py` 运行六组研究方案。

- [研究与交付路线](docs/ROADMAP.md)
- [逐桶扩容研究模板](docs/ai-prompts/bucket-expansion.md)
- [初始化与验证记录](docs/INITIALIZATION.md)
- [统一工程与量化规范](AGENTS.md)

计划用 `v4.0.0` 标记本次源程序与工程初始化基线。当前远端标签尚未创建，代码基线已固定在 [92c1af0](https://github.com/yuan2go/ashare-leader-rotation/commit/92c1af0f605fa012702dab3097bbee708ade0963)，该提交的 Windows / Ubuntu [CI 已通过](https://github.com/yuan2go/ashare-leader-rotation/actions/runs/45116874098)。研究结论仍须对应真实输入和运行证据。
