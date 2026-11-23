# ashare-leader-rotation

A 股跨行业龙头周频趋势轮动研究台 v4.0，包含独立的宽基 ETF 资金雷达。通过可追溯输入、固定规则和 Python / 离线 JavaScript 对账，研究信号如何转化为实际可执行的扣费账户表现。

**v4.0 联网采集覆盖固定池全部上市日。差异化权益支持公告折算校验，正式回放状态见 [最新数据验收](results/rotation_v4_0_validation.md)。**

**v4.1 离线前端**沿用文件名 `rotation_quant_lab_v4_0_offline.html`。五个页面共用冻结引擎，股票矩阵按 `universe.csv` 扩展。宏观展示桶用于筛选和配色，跨桶策略按配置中的原桶执行。[参考原型](docs/ui/rotation_quant_lab_v4_1_ui_prototype.html)中的数据为演示数据。

## 原包导入基线

2026-11-23 从 `rotation_v4_0_python_backend.zip` 导入，原策略、配置与研究结果保留；两份冻结引擎的字节哈希与初始化蓝图一致。详见 [初始化与验证记录](docs/INITIALIZATION.md)。

| 项目 | 当前状态 |
| --- | --- |
| 股票池 / ETF | 16 只种子股，当前 7 个股票桶；7 只宽基 ETF |
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

构建器将 `results/research/<六位代码>.json` 中版本为 1、代码吻合且 `asof` 等于冻结日的插件注入 `RAW.research`。插件的 `experiments` 按 `earnings_event_study` / `price_down_flow_in` 协议渲染。资金、轮动选股与撮合读取原输入表和冻结规则。

### 联网补齐真实数据与回放

准备文件后，安装原包声明的采集依赖，再分别检查采集与正式回放的退出状态。以下使用已激活环境中的 `python`；Windows 未激活时用 `.\.venv\Scripts\python.exe` 替换：

```bash
python -m pip install -r requirements.txt
python fetch_v4_data.py
python run.py --confirm-seeds
```

采集使用 BaoStock、公开行情接口、上交所日份额接口和 AKShare 所连接的深交所日规模报表。`--stocks-only` 可复核股票和权益并保留现有 ETF 表。差异化分派可在 `data/verified_actions_override.csv` 录入公告折算值。两端以该底稿逐条匹配折算字段，并按原 ¥0.015 容差验算参考价。账本仍按普通股东实际权益入账。

交易所历史日规模表标记 `exchange_t1_convention`，统计日之后的首个真实交易日为逻辑可见日，源回执保留实际下载时间。`verified_live` 要求可核验的北京时间发布时刻，15:00 后披露从后续交易日生效。历史数据的 T+1 是研究假设。冻结日统计的份额要到下一交易日才进入雷达。停牌无价的日期保留原始份额回执，联合计算使用覆盖整个窗口的固定成员。

也可先从仓库保留的核验底稿重建当前部分数据，无需行情 SDK 或联网：

```bash
python fetch_v4_data.py --offline
python run.py --confirm-seeds
```

没有本地缓存的新克隆仍会报告股票缺口。有联网底稿时，正式验收继续检查公告、参考价和两端因果回放。普通 `python run.py` 可在数据不足时构建诊断页并退出 0，正式验收应使用 `--confirm-seeds` 并检查审计字段。

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

四窗全池回放结果用于评估是否扩容。八桶与 50 只目标仍处于研究规划。

- [研究与交付路线](docs/ROADMAP.md)
- [逐桶扩容研究模板](docs/ai-prompts/bucket-expansion.md)
- [初始化与验证记录](docs/INITIALIZATION.md)
- [统一工程与量化规范](AGENTS.md)

计划用 `v4.0.0` 标记本次源程序与工程初始化基线。当前远端标签尚未创建，代码基线已固定在 [92c1af0](https://github.com/yuan2go/ashare-leader-rotation/commit/92c1af0f605fa012702dab3097bbee708ade0963)，该提交的 Windows / Ubuntu [CI 已通过](https://github.com/yuan2go/ashare-leader-rotation/actions/runs/45116874098)。研究结论仍须对应真实输入和运行证据。
