# 历史研究脚本

这些命令记录一次性实验。旧研究回执保留其运行时输入和输出身份。归档前脚本文件的 SHA-256 如下。

| 原路径 | 当时的输入与用途 | 迁移前 SHA-256 |
| --- | --- | --- |
| `research/exp_drop_300308_stress.py` | v4.0 导入种子池与冻结行情，比较移除 300308 前后的回放。 | `9e108661983b9e1d07ed56cb01e8b0a321d2b5d9cea60f7b22899803013e32f5` |
| `research/exp_batch1_pool_comparison.py` | Batch 1 扩池与 data/universe_seed16_backup.csv 的原 16 股对照，输入签名见 batch1_pool_comparison 回执。 | `88620b8784bcd2989a1b818cfa8c3b267e21094f504361f0ea690ea2d75c96d4` |
| `scripts/audit_kt2_turnover_t2_attribution.py` | 使用命令行提供的 T+1、T+2 成交和 NAV 表及可选行情目录，复算 Kt2 换手与摩擦归因。 | `176d7e465d4dbce7ef51700beb08c40c0386200810e6b0a5a6b67c6b0b78cebd` |

## 原始输入回执

前两个回执的 `input_sha256` 逐表记录研究输入。第三个 manifest 指定原始窗口、双袖子配置及账本。可按以下 SHA-256 定位对应回执。

- `results/research/drop_300308_stress.json`　`71f3d5b122f8421f8f0420dd1497746c343e2234c9a8625c1ef6275d2d159abc`
- `results/research/batch1_pool_comparison.json`　`d7662933ee53588591bdf03354f39bd10cb4cb3dc56f94ac20456406382fe8e5`
- `reports/phase2b_p1_attribution/inputs/input_manifest.json`　`b35edf5c5e7b58d18f832b53b4d60c2a760fdee6141df9fa6cbba3dda0d245d7`

旧 Kt2 归因工具默认防御槽数为 5，W_Bear 默认区间为 2021-02-18 至 2024-02-05。复查 P1 的 50/50 BASE 时，用 `--defense-slots 3`，并通过 `--windows` 指定 policy 中的 2021-03-01 至 2023-11-19。输入 manifest 记录实际使用的参数。

## 600428 历史插件

`results/research/600428.json` 保留作旧实验回执。其 stocks、actions 签名与当前工作区不符。盈余与资金流反转实验已退出主线，正式离线发布按输入签名拒绝此插件。建议随历史实验归档，重启该研究时再签发新回执。
