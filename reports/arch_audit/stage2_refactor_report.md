# 阶段 2 架构重构与验证记录

核验日期为 2026-11-24。**36 组 BASE 与 8 组 M200_HYST_HALF 全部逐日逐笔一致，差异行数为 0。** 每组先验归档 CSV 的 SHA-256，再按原顺序比较，缺行、多行、重复或乱序都会使检验失败。

| 影子对账 | 组合 | NAV 行 | 成交行 | 差异行 |
| --- | ---: | ---: | ---: | ---: |
| BASE，50/49 股 × W1、IS、OOS、W_Bear、W5、2026 × 2bp、7bp、T+2 | 36 | 44,058 | 20,907 | 0 |
| M200_HYST_HALF，50/49 股 × IS、OOS、W_Bear、W1 × 2bp | 8 | 11,896 | 5,580 | 0 |

NAV 逐日对齐权益、现金及双袖子持仓。成交逐笔对齐交易日、信号日、代码、方向、袖子、股数、价格、名义金额、税佣、滑点、成交后现金与卖单归因。金额均转为整数分。BASE 同时核对归档统计摘要与历史年度换手字段。

## 工程变更

| 范围 | 实施结果 |
| --- | --- |
| K01–K03、E03、E08 | 机制、风险与 Barbell 原有 3 处研究日循环合并为 `core.research_ledger.run_sessions`。单袖子与双袖子分别提交计划及开盘订单，共用现金、成交、分红应收到账、待上市股和收盘市值计算。成交调用冻结引擎的整手、费用和限价原语。 |
| 策略与 S15 | 防御预留、趋势保留缓冲、死区和滞回风控通过普通函数调用。P1、P2、模型审计全部移除动态执行及函数全局变量替换。市场滞回状态从预热期逐日延续，组合回撤刹车使用本窗口 NAV。静态序列覆盖等于边界、跨越边界和保持原状态。 |
| K04–K08、D03 | 交易月末、完整自然月末周信号、锚定双周采用独立接口。AnnVol20 使用 21 个可交易复权收盘、20 个对数收益的样本标准差与 √252。CAGR 使用自然日差 / 365.25。窗口单边换手等于总成交额 / 两倍日均权益，真年化再乘 242 / 交易日数。旧年度摘要保留先求浮点平均净值的舍入顺序。 |
| E01、E02 | 例行 P1、P2 诊断、P2 消融、模型审计分别哈希 36、31、119、54 个明确输入文件，缓存路径数均为 0。独立全量缓存取证入口为 `scripts/cache_forensics.py --full-cache`。泄漏扫描核验暂存 JSON/MD 的最终字节。 |
| S07、S10、S20、D12、E07 | 锁定候选与负对照必须非空且实际参加核查。三股校准按逐股覆盖、价格、成交量与流动性分项验收，绑定日历、行情、权益、证券信息切片。成交量容差为 100 股，除权参考价按原舍入及 ¥0.005 容差验证。有冻结成交额时校验 252 日均额相对误差，无该列时采用日均 3 亿元门槛。删除 `market_index` 的空置参数。 |
| E04、D01–D11 | 三个一次性脚本归档，两个报告树入口转发至 scripts。文档区分 16 股导入基线与 50 股工作区，写明 3 个防御槽、窗口边界、摘要容差、HTML 历史身份、Node 两种入口和 CI 数据约束。 |

600428 历史插件在归档说明中标记过期。当前 `stocks` 签名由 `27edface…` 变为 `f86b024b…`，`actions` 由 `84cb0f5f…` 变为 `0d34a119…`。旧 JSON 原样保存。

## 验证

实际运行环境为 `D:\projects\ashare-leader-rotation\data\fetch_cache\runtimes\batch1-py311\Scripts\python.exe`（3.11.18）和 Node v24.22.3。Python 命令均使用 `-B -X utf8`。

| 命令或检验 | 结果 |
| --- | --- |
| `scripts/check_integrity.py` | 退出 0 |
| `-m unittest discover -s tests -v` | 55 个测试通过，0 跳过。含 4 个 Golden Master、静默通过负例和新增 7 个 core 测试方法。44 个归档组合在一个矩阵测试中逐组断言。退出 0 |
| `node tests/parity.cjs` | 退出 0。重放同一合成包，36 条路径、102,132 行、6,502 笔成交。该重复入口单列，不计入历史影子对账数量。 |
| 隔离导出 | P1、P2 各完成 216 组，P2 同时通过 11 个摘要锚点与 36 个前缀检查。P2 诊断通过。模型审计的初始复核与 8 份归档账本一致，生成 90 行内存归因统计。滚动复验完成 108 份账本、72 项端点检查及 24 份前次导出比对。研究导出均写入临时目录。 |
| 额外单袖子复核 | 在 2025-12-01 至 2026-11-20 窗口、额外延迟一日及 7bp 条件下，对 9 个机制和 4 个风险模型比较迁移前后的完整返回值，全等。 |
| manifest 耗时 | 正式 8 表、daily_basic、冻结文件和脚本合计 16 个文件。本机两次实测 0.122 秒、0.116 秒。 |
| 三股校准 | 用本地缓存独立生成临时回执，再用消费者逻辑验签。600989 使用冻结成交额对照，600900 和 002371 使用替代流动性门槛。现存历史回执缺少新切片签名，当前采集门禁将要求重新签发。 |
| 静态复核 | 活跃 Python 源码 52 个文件，动态 exec、getsource、FunctionType 调用均为 0，指向三个归档脚本的活跃导入为 0。 |

正式市场回放状态取自既有 `results/data_audit.json`。其 `stock_issues=0`、`etf_issues=0`、`production_replay=true`。本次独立执行研究回放，未启动重写正式结果的 `run.py`。50 股行情共 136,476 行，市场日历为 2015-01-05 至 2026-11-20 的 2,887 日。7 只 ETF 共 11,466 行，11,459 行在冻结期内可用。六只从 2020-01-02 开始，588000 从 2020-11-16 开始，末个可用观察日均为 2026-11-19。

本地完成了 CI 所调用的验证命令。远端流水线未触发。在无正式输入的干净检出中，CI 需要仓库指定的签名行情和归档包，具体变量见 README。

## 冻结与历史证据

SHA-256 边界包含六个核心文件、全部 data/（含 21,550 个缓存文件）、results/、reports/phase2b_* 和 reports/model_reality_audit/。报告目录仅排除获准迁移的两个 Python 入口。运行前后均为 22,797 个文件。按相对路径排序的 SHA-256 映射摘要为 `96894260fba349d8a0cddb49a482a9d58113686e31c70e54f3635a328d717300`，前后断言通过。

| 文件 | 实测 SHA-256 |
| --- | --- |
| engine.py | `090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80` |
| offline/engine.js | `26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945` |
| engine_integrity.json | `441c57845c62484caeef7ef87b610371e3ab40d71b649bc2807be5adc70f18c0` |
| policy.json | `3dc3639a1b498c2e1620a9d5eecb18da477357293005f75826dcdd8bd717f55a` |
| inputs.py | `3d7368c679d4ed6935bbd0769cd0acf96f29b5a6bdbe9ba77d46de0878dc2c75` |
| offline/input.js | `ee9336ef3e41da02d0dd1e9045eeb6e623465d6ec1727d52123b38ebd3b2ffca` |

## 本阶段文件清单

| 分组 | 仓库相对路径 |
| --- | --- |
| 新增共享模块 | core/calendar.py、core/metrics.py、core/research_ledger.py、core/manifest.py、core/research_support.py、core/calibration.py |
| 采集与筛选 | fetch_v4_data.py、research/final_pool_screen.py、research/verify_tushare_baseline.py |
| 研究账本与比较器 | research/exp_50_pool_barbell.py、research/exp_50_pool_topk_diag.py、research/exp_v5_a2_risk_control.py、research/exp_v5_mechanism_study.py、research/exp_50_pool_v5_precheck.py、research/k2_divergence.py |
| 因子与验收 | research/factor_lab/court.py、research/factor_lab/run_phase2a.py、research/factor_lab/run_phase2a_plus.py、research/exp_50_pool_phase2b.py、research/exp_50_pool_phase2b_p2.py |
| 研究入口与取证 | scripts/run_phase2b_p1_ablation.py、scripts/diag_phase2b_p2_regimes.py、scripts/audit_model_reality.py、新增 scripts/cache_forensics.py |
| 报告树入口迁移 | scripts/eval_phase2b_p1_ablation.py、scripts/run_rolling_retest.py。原路径 reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py、reports/phase2b_p1_ablation/rolling_retest/run_rolling_retest.py 保留转发 |
| 归档 | archive/legacy_research/ 下的 exp_drop_300308_stress.py、exp_batch1_pool_comparison.py、audit_kt2_turnover_t2_attribution.py、README.md。原路径及历史输入签名见该 README |
| 回归、文档、CI | 新增 tests/test_core_ledger.py，更新 tests/test_fetch.py、tests/test_factor_lab.py、tests/test_silent_pass_guards.py、AGENTS.md、README.md、.github/workflows/ci.yml，新增本报告 |
