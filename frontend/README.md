# Phase 7B · 影子交易驾驶舱

使用 Node 24.22.3、npm 10.9.4 和仓库的 Python 3.11.18 环境。前端依赖锁定在 `package-lock.json`，接口类型由 Phase 11 规范 `../backend/openapi_phase11.json` 生成。接口说明见 [Phase 11 API 契约](../docs/api_contract_phase11.md)。

## 本地运行

从仓库根目录启动 FastAPI。

```powershell
powershell.exe -NoProfile -File scripts/run_daily_shadow.ps1 -Action ServeApi
```

开发时在另一个 PowerShell 终端运行以下命令，再打开 `http://127.0.0.1:5173`。Vite 会将 `/api` 转发给本机 8000 端口。

```powershell
$env:Path = (Resolve-Path 'data/fetch_cache/runtimes/node_modules/node/bin').Path + ';' + $env:Path
Set-Location frontend
npm ci
npm run dev
```

当前 Windows 工作区若仍解析到系统 npm 6，使用本地的 npm 10 CLI，例如 `node .npm-tools/cli/package/bin/npm-cli.js run dev`。安装、构建与测试均从 `frontend/` 执行。

运行 `npm run build` 生成 `frontend/dist/` 后，重启 FastAPI，再打开 `http://127.0.0.1:8000`。FastAPI 从同一地址提供页面和接口。

## 验证

```powershell
npm run check:types
npm run typecheck
npm test
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
npm exec -- playwright install chromium --only-shell
npm run test:e2e
```

浏览器测试默认启动使用签发账簿的本机 FastAPI，限制浏览器请求为本机只读请求。以上命令将锁定 Playwright 版本对应的 Chromium 安装进前端依赖目录，也可通过 `PHASE7B_BROWSER_EXE` 使用已安装的兼容 Chromium。需要保存桌面与移动端验收截图时，将 `PHASE7B_SCREENSHOTS` 设为输出目录。

## 使用口径

- 全局资金档适用于影子曲线、持仓成交和支持资金筛选的组合比较。开盘指令单使用签发的 ¥45 万主档。逐年收益和回撤事件使用各自档案的资金口径。
- 净值、收益、回撤、超额、波动率、均线、偏离度、股数和各项费用来自 API。前端负责格式化、筛选和绘图。
- 运维页面通过 API 提交任务并轮询日志。采集和采集后入账要求在表单中单独确认行情访问。自动化验收使用前端模拟响应或本机只读服务。
