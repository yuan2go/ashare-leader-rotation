# 阶段 1 门禁与 Golden Master 验收记录

2026-11-24，本地工程验收通过。48 个 unittest 全部通过，其中静态 Golden Master 4 个、门禁回归 15 个。受保护清单共 22,799 个文件，按相对路径和 SHA-256 核对，新增、缺失、字节变化均为 0。

## 门禁与反例

| 编号 | 生效位置与验证结果 |
| --- | --- |
| S01 | 完整性 CLI 与正式入口共用批准的两项锁键、两项引擎摘要和三项协议摘要。空锁、漏键、多键、锁值变动及磁盘协议漂移均阻断。 |
| S02 | 回执缺失、JSON 损坏、缺少 `blockers`、类型错误、列表非空时，在读取行情和启动回放之前终止。阻断批次的审计、研究 JSON、报告和页面记录原因，`production_replay=false`，退出 1。 |
| S03 | 单行 BASE 汇总 CSV 的收益漂移 +0.20 个百分点时，写出 DRIFT、缺失锚点、空排名和失败报告，打印 `[FAIL]`，退出 1。原 JSON 清单入口同样生效。 |
| S04 | 五个研究入口共用 3.11 与优化模式守卫。对守卫及五个实际入口分别启动 `-O` 子进程，均命中指定 RuntimeError。 |
| S05 | 快速复用逐项核验行情表、两份必需证据和采集回执。空、缺项和未知证据清单进入完整校验路径。 |
| S06 | 研究输入按八张正式表、各入口约定的因子文件和证据文件核验。Top-K 与 P1 导出器的删键测试均阻断签发。 |
| S08 | 结构判断要求 50 股与 49 股双池、完整滚动窗、约定模型和执行场景。空池、单池、缺 T+2 均被拒绝。 |
| S09 | W1 与 OOS 须各有正样本数和有效相关系数。缺窗、零样本和非有限值返回 `INSUFFICIENT_EVIDENCE`。 |
| S12 | 排名前验证 6 变体 × 2 池 × 2 核心场景的 24 个 IS 单元。少一个单元或重复一个单元均输出失败和空排名。 |
| S17 | 单票插件的签名至少覆盖日历、指数、行情、权益事件，并比对额外声明的键。现有 600428 插件的行情和权益事件摘要过期，隔离构建已跳过该插件。 |
| S19 | 正式消融验收要求交易与 NAV 文件、必需列及成交当日的正净值，未知买卖方向直接失败。缺文件、缺列、UNKNOWN 均实测退出 1。 |
| S21 | `approved_policy` 读取策略状态，`explicit_confirm` 记录命令行标志，`production_replay` 记录回放完成情况。兼容字段 `approved` 为前两者的逻辑或，`confirmed` 映射到显式确认。 |
| S30 | 在输出盘暂存、校验四份文件，封存带摘要的批次，再原子替换 `current_batch.json`。旧路径由单写者更新，更新和回滚期间暂置非通过状态。在报告、HTML、审计和批次指针四个替换点注入异常，旧批次、指针与镜像逐字节恢复，暂存及失败批次均已清理。 |

需要一次读取多份同批产物的调用方可读取一次 `results/current_batch.json`，再从其中的 `batch` 路径读取并核对摘要。旧版根目录 HTML 与各结果文件位于不同路径，各自使用单文件原子替换。

## 四组独立账本

| 组 | 静态预期与实测范围 |
| --- | --- |
| GM-1 | 挂单跨停牌、下一可成交开盘、上下限夹价、2bp 取整和不足一手。两次卖出均为 100 股 × 10,000 分，佣金各 500 分，印花税从 1,000 分降至 500 分。Python 和 JS 逐笔、逐日一致。把首笔佣金改为 501 分会触发断言。 |
| GM-2 | 双标的账本中，100 股在停牌除权日产生 4,500 分应收和 10 股待上市股份。原股复牌卖出、现金到账、余股解锁分别发生。Python 逐日直接核对可用现金、应收、已结算与待上市股数。两端核对成交与权益，并拒绝错误参考价和非因果公告。 |
| GM-3 | 合成日历中，2024 年 1 月 24 日周三为短周信号日，1 月 31 日是自然月末。2bp / 7bp 均于 1 月 29 日执行，T+2 于 1 月 30 日执行。Python 运行实际 Barbell 双袖子账本。JS 用浏览器冻结撮合器分别执行本例的两个单槽，验证预留后的成交和合计权益。两端分别从截断至 1 月 31 日的原始表重新 prepare，逐日核对排名、成交与精确到分的现金和权益。 |
| GM-4 | 归档 50 股 W1/2bp/BASE 的 2016-01-04 至 01-29，共 20 日、5 笔交易。用成交重建现金和两袖子持仓，逐日核对权益变动。期初 45,000,000 分，加已实现 266,000 分、未实现 −3,194,000 分，扣佣金 10,781 分、税 10,678 分，期末 42,050,541 分。整份源 CSV、切片原字节、静态夹具均核对 SHA-256。 |

## 执行记录

Python 为 `D:\projects\ashare-leader-rotation\data\fetch_cache\runtimes\batch1-py311\Scripts\python.exe`（3.11.18）。系统默认 python 为 3.12.10，`py -3.11` 未找到解释器。Node 采用本机可用的 24.22.3。所有 Python 验证使用 `-B -X utf8`，无参数 Node 对账启动的子进程也使用同一解释器和参数。

| 验证 | 退出码 | 实际范围 |
| --- | ---: | --- |
| `scripts/check_integrity.py` | 0 | 仓库原位及验收副本均通过 |
| `scripts/prepare_workspace.py` | 0 | 仅在临时副本中用当前源码重建离线页面 |
| `-m unittest discover -s tests -v` | 0 | 48/48，覆盖 test_engine、test_fetch、test_phase2b、test_factor_lab 及两份新增模块 |
| `node tests/parity.cjs` | 0 | 3,098 日状态、36 条路径、102,132 行账本、6,502 笔成交记录。与 unittest 重叠的合成对账只计一次 |
| 归档 P1 清单运行验收器，`--out-dir` 指向临时目录 | 0 | 读取 216 条既有记录，11 个 BASE 锚点通过，6 个变体可排名 |
| 运行后保护清单复核 | 0 | 1,312,758,308 字节，包括 data/ 21,577 个、results/ 35 个、指定历史报告区 1,180 个及 7 个单列文件 |

`results/synthetic_verification.json` 前后均为 `a2d528a0152149bca7adf606bd9e3406bdafe32eeb8b783f61b51305508bd3e7`。保护清单按相对路径排序后的规范 JSON 摘要为 `6e11b6f6382a1a2eac292d6b1d1f4aadfffc96f7d2232d37837f931b4169088d`。

| 冻结或只读文件 | 前后实测 SHA-256 |
| --- | --- |
| engine.py | `090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80` |
| offline/engine.js | `26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945` |
| engine_integrity.json | `441c57845c62484caeef7ef87b610371e3ab40d71b649bc2807be5adc70f18c0` |
| policy.json | `3dc3639a1b498c2e1620a9d5eecb18da477357293005f75826dcdd8bd717f55a` |
| inputs.py | `3d7368c679d4ed6935bbd0769cd0acf96f29b5a6bdbe9ba77d46de0878dc2c75` |
| offline/input.js | `ee9336ef3e41da02d0dd1e9045eeb6e623465d6ec1727d52123b38ebd3b2ffca` |
| research/exp_50_pool_barbell.py | `8fc72165095cf4e7e92234b7adab0ddf1f9ed0c4bc94da66234b5b9ba4ab24d0` |

Barbell 工作区源码与 P1 导出器的现有 `BASE_BOOK_SHA` 一致。历史 manifest 的源码摘要以 `29a0e8eb` 开头，此差异在本轮开始前已存在。归档验收读取已导出账本，本次未重新生成这些交易。

## 文件清单与研究范围

- 本轮新增 `tests/test_golden_masters.py`、`tests/test_silent_pass_guards.py`、`reports/arch_audit/stage1_hardening_report.md`。
- 在已有工作区改动上补全 `core/integrity.py`、`core/publication.py`、`run.py`、`fetch_v4_data.py`、`research/common.py`、`research/exp_50_pool_topk_diag.py`、`research/exp_50_pool_phase2b.py`、`research/factor_lab/run_phase2a.py`、`research/factor_lab/run_phase2a_plus.py`、`scripts/audit_model_reality.py`、`reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py`、`tests/parity.cjs`、`tests/golden_books.cjs`、`tests/fixtures/golden_base_slice.json`。
- `core/__init__.py`、`scripts/check_integrity.py`、`scripts/run_phase2b_p1_ablation.py`、`research/factor_lab/court.py` 已有本阶段对应实现，本轮验收覆盖这些入口。

历史 `results/data_audit.json` 记载 50 股、136,476 行，`stock_issues=0`、`etf_issues=0`、`production_replay=true`。7 只 ETF 共 11,466 行，冻结时可用 11,459 行。159915 为 1,667/1,666 行，其余五只从 2020-01-02 起覆盖的 ETF 各 1,668/1,667 行。588000 从 2020-11-16 起为 1,459/1,458 行。各基金观测截止 2026-11-20，最后可见观测日为 11-19。

本轮仅在临时副本重建前端，未执行正式全池回放、远端 CI、前瞻验证或实际交易。阶段 2 的目录迁移和账本循环拆分仍待后续授权。
