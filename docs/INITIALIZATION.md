# v4.0 仓库初始化与验证记录

记录日期：2026-11-23（UTC+8）。本记录说明原始交付包如何进入仓库、初始化变更范围及实际验证，不将工程完成写成策略收益验证。

## 来源与不变范围

- 源包：rotation_v4_0_python_backend.zip，631,472 字节。
- 源包 SHA-256：`804b7315fb61efb2eb16c952103fe531399da9d7ee518f5f50d302659c7f1f83`。
- 将 rotation_v4/ 下内容平铺到仓库根；业务程序、离线源码、原测试、配置、输入底稿和原报告按原文件导入。
- 原 README 保存在 [归档说明](archive/v4.0-source-README.md)；原包全部文件的字节哈希保存在 [导入清单](archive/v4.0-source-manifest.json)。该清单包含有意不入 Git 的生成文件和大体积行情，仅用于来源追踪，不取代引擎冻结锁。
- 不修改策略、参数、股票池、基金池或研究日期，不执行联网补数或正式实盘交易。

| 冻结文件 | 实测 SHA-256 |
| --- | --- |
| engine.py | `090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80` |
| offline/engine.js | `26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945` |

policy.start=2015-01-01 与第一交易日 2015-01-05 含义不同；保留原配置。policy.status=approved 表示既有研究配置审批状态，不表示缺失数据已通过验收。

## 初始化变更

1. 增加统一 AGENTS.md 与 Claude / Cursor 引用入口；原包数据契约归档可查。
2. 增加 README、本记录、路线图和逐桶扩容模板，区分已导入能力、工程验证与尚待完成的真实研究。
3. 增加 .gitignore、.gitattributes，隔离可重拉行情、采集缓存、凭据与生成页面，同时保护冻结文件和输入底稿的原字节。
4. 增加 check_integrity.py：固定批准的两项 v4.0 哈希，先检查锁文件，再检查实际字节，拒绝引擎和锁同时静默换值。
5. 增加 prepare_workspace.py：为缺失行情文件补表头，使用原生页面构建函数生成离线页；不覆盖已有行情或正式审计报告。
6. 增加 GitHub Actions：Ubuntu / Windows、Python 3.11 / Node 24、UTF-8、只读权限、完整提交 SHA 固定的官方 Actions。工程测试仅使用标准库，不访问行情服务。

## 本地干净环境工程验证

在隔离副本中排除 data/stocks.csv、data/etf.csv 和生成 HTML，模拟新克隆。实际环境 Python **3.12.14**、Node **24.22.3**；GitHub 配置的 Python 3.11 与 Windows 结果以对应提交的 Actions 运行为准。

| 实际命令 | 退出码 | 核验内容 |
| --- | --- | --- |
| python scripts/check_integrity.py | 0 | 两份冻结字节与批准锁一致 |
| python scripts/prepare_workspace.py | 0 | 缺失表头与离线页成功生成；未覆写正式报告 |
| python tests/test_fetch.py | 0 | 6 项既有无网采集适配测试通过 |
| python tests/test_engine.py | 0 | 原生 Python、JS parity、前缀与 UI 验证通过 |
| python scripts/check_integrity.py | 0 | 验证后冻结字节仍一致 |

合成验证覆盖 3,098 个市场日、36 条路径、102,132 条账本行、6,501 笔成交，并验证配置扩至 50 个测试代码。它们全部是工程合成样本。test_engine.py 已调用 parity 与 UI，不再重复运行无参数 parity。

工程准备和测试前后，原 results/data_audit.json 与 results/rotation_v4_0_validation.md 的字节哈希保持一致。

冻结核验还在隔离副本中进行必要的负例检查：改变引擎字节返回 1；同时改变引擎与锁文件为相互匹配的新哈希仍返回 1；仅替换锁文件返回 1；恢复批准文件后返回 0。没有为这些检查改变真实业务文件。

## 保留底稿的原生重建与回放验证

另一个隔离的新克隆条件副本，从表头开始，使用仓库内 data/verified/ 原始核验底稿重建；不依赖相邻的牧原项目目录、不联网、不使用合成行情。

| 实际命令 | 退出码 | 结果解释 |
| --- | --- | --- |
| python scripts/prepare_workspace.py | 0 | 初始化缺失输入与离线页 |
| python fetch_v4_data.py --offline | 1 | 重建可用真实底稿；原有股票缺口仍存在 |
| python run.py --confirm-seeds | 1 | 正式全池未通过；合格的牧原 T1 / 沪深 300 独立对照完成原生回放及双端校验 |

本次原生重建后的审计：

| 字段或范围 | 实测值 |
| --- | --- |
| 日历 | 2,887 日，2015-01-05—2026-11-20 |
| stock_issues | 15 |
| 缺失股票记录 | 40,693 |
| 已有股票记录 | 2,887，全部为牧原 |
| 指数缺失记录 | 0 |
| etf_issues | 0（即 len(etf_errors)，不是覆盖完成指标） |
| ETF 记录 / 冻结日可用 | 1 / 0 |
| production_replay | false |
| 独立对照双端核验 | 2,887 个市场日、32 条路径、37,600 条账本行、426 笔成交 |

新重建结果与导入报告的缺口一致。导入时的八张原始输入 CSV 的哈希也已与原包 data_audit.json 对应值核对一致；此项字节核对不指向后来重新生成的 CSV。上述来源证明和原生回放不等于重新向交易所核实了全部历史行情。

## 状态边界与后续

- 工程初始化与必要本地验证已完成；远端以本次提交的 [GitHub Actions](https://github.com/yuan2go/ashare-leader-rotation/actions) 实际结果为准。
- 原包审计与报告保留在 results/，对应初始化版本。v4.0.0 为源程序和工程基线标签，后续重跑不得覆盖这一历史身份。
- 联网行情 SDK 安装、行情服务可达性、其余 15 只股票补数、完整股票轮动结果、ETF 连续日份额和自然前瞻表现未在本次初始化完成。
- 下一阶段保持既有规则，先补数据和处理真实异常，再完成四窗与实际执行诊断；扩池安排见 [ROADMAP](ROADMAP.md)。
