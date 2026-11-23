# CLAUDE.md

先完整阅读并遵守根目录 [AGENTS.md](AGENTS.md)。引擎冻结、因果/PIT、配置边界、数据证据、双端一致性与交付要求均以该文件为准。

开始修改前读取 `README.md`、`policy.json` 与本次相关源码；需要数据状态时核对 `results/data_audit.json` 的来源、输入哈希和 `production_replay`，不要把导入基线当成当前本地数据。

新克隆的准备与验证命令见 README。遇到数据错误先定位真实代码、日期和公告，不通过调容差或更新冻结哈希使任务通过。默认用中文说明改动、实际验证与剩余缺口。
