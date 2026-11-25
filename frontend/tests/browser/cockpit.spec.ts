import { expect, test } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import type { components } from "../../src/api/schema";

type Overview = components["schemas"]["Overview"];

test.beforeEach(async ({context, baseURL}) => {
  if (!baseURL) throw new Error("Loopback base URL required");
  const origin = new URL(baseURL).origin;
  await context.route("**/*", async (route) => {
    const request = route.request();
    if (new URL(request.url()).origin !== origin || !["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      await route.abort();
      throw new Error(`Unexpected browser request ${request.method()} ${request.url()}`);
    }
    await route.continue();
  });
});

test("desktop shows all six accounts and switches to signed numbers for either capital", async ({page}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  const module = await page.locator("script[type=module]").getAttribute("src");
  if (!module) throw new Error("Missing frontend entry module");
  expect((await page.request.head(module)).headers()["content-type"]).toContain("javascript");
  await expect(page.getByRole("heading",{name:"实盘影子驾驶舱"})).toBeVisible();
  await expect(page.getByRole("status").filter({hasText:"API 正常"})).toBeVisible();
  await expect(page.getByLabel("账簿来源状态")).toContainText("历史补采");
  await expect(page.getByLabel("账簿来源状态")).toContainText("等待上游发布");
  await expect(page.getByRole("region",{name:"六线账户对照"}).getByRole("row")).toHaveCount(7);
  const chart = page.getByRole("img",{name:"六线净值曲线"});
  await expect(chart.locator("canvas").first()).toBeVisible();
  await expect(page.getByText("57.30%")).toBeVisible();
  const screenshots = process.env.PHASE7B_SCREENSHOTS;
  if (screenshots) {
    await mkdir(screenshots,{recursive:true});
    await page.screenshot({path:join(screenshots,"dashboard-desktop.png"),fullPage:true,animations:"disabled"});
  }
  const overview = await (await page.request.get("/api/v1/shadow/overview")).json() as Overview;
  const v5 = overview.accounts.find((row) => row.account === "V5" && row.capital_rmb === 2000000);
  expect(v5).toBeDefined();
  const nav = new Intl.NumberFormat("zh-CN", {style:"currency",currency:"CNY",currencyDisplay:"narrowSymbol"})
    .format((v5?.nav_cents ?? NaN) / 100);
  await page.getByRole("button",{name:"¥200 万"}).click();
  await expect(page.locator(".kpi-value").first()).toHaveText(nav);
  await page.getByRole("button",{name:"回撤",exact:true}).click();
  await expect(page.getByRole("img",{name:"六线回撤曲线"})).toBeVisible();
  expect(errors).toEqual([]);
});

test("routes, refreshes, CSV/Markdown downloads, audit tabs and archive tabs work against the live contract", async ({page}) => {
  await page.goto("/");
  await page.getByRole("link",{name:"开盘指令单",exact:true}).click();
  await expect(page.getByRole("heading",{name:"次日开盘指令单"})).toBeVisible();
  await page.reload();
  await expect(page.getByText("OBSERVATION ONLY · 探索盘勿实盘重仓")).toBeVisible();
  for (const [format, label] of [["csv","下载 CSV"],["md","下载 Markdown"]] as const) {
    const pending = page.waitForEvent("download");
    await page.getByRole("button",{name:label}).click();
    const download = await pending;
    expect(download.suggestedFilename()).toBe(`next_open_order_ticket.${format}`);
    const downloadPath = await download.path();
    if (!downloadPath) throw new Error("No download file");
    const bytes = await readFile(downloadPath);
    expect(bytes.equals(await (await page.request.get(`/api/v1/tickets/next-open.${format}`)).body())).toBe(true);
  }
  expect((await page.goto("/portfolio"))?.status()).toBe(200);
  await page.getByRole("combobox",{name:"账户",exact:true}).selectOption("PIT300_ex688_EW_PROP");
  await page.getByRole("button",{name:"成交",exact:true}).click();
  const fills = page.getByRole("region",{name:"成交明细"});
  await expect(fills.getByRole("row")).toHaveCount(26);
  await page.getByRole("button",{name:"下一页"}).click();
  await expect(page.getByText("第 2 页")).toBeVisible();
  await expect(fills.getByRole("row")).toHaveCount(26);
  await page.getByRole("button",{name:"公司行动",exact:true}).click();
  await expect(page.getByText("该筛选条件下暂无记录")).toBeVisible();
  expect((await page.goto("/research"))?.status()).toBe(200);
  await expect(page.getByRole("region",{name:"研究组合比较"}).getByRole("row")).toHaveCount(7);
  await page.getByRole("button",{name:"逐年收益",exact:true}).click();
  await expect(page.getByRole("img",{name:"年度收益柱状图"})).toBeVisible();
  await page.getByRole("button",{name:"回撤事件",exact:true}).click();
  await expect(page.getByRole("region",{name:"回撤事件"})).toBeVisible();
  await page.getByRole("button",{name:"因子法庭",exact:true}).click();
  await expect(page.getByRole("img",{name:"五分组收益"})).toBeVisible();
  await page.getByRole("button",{name:"置换检验",exact:true}).click();
  await expect(page.getByRole("region",{name:"置换检验档案"})).toBeVisible();
  await page.getByRole("button",{name:"数据覆盖",exact:true}).click();
  await expect(page.getByRole("region",{name:"ETF 覆盖明细"}).getByRole("row")).toHaveCount(6);
  expect((await page.goto("/ops"))?.status()).toBe(200);
  await expect(page.getByText("签发文件校验通过")).toBeVisible();
  await expect(page.getByRole("region",{name:"采集状态"})).toBeVisible();
  await page.getByRole("combobox",{name:"任务类型",exact:true}).selectOption("Collect");
  await expect(page.getByRole("button",{name:"提交任务"})).toBeDisabled();
  expect((await page.request.get("/api/v1/no-such-resource")).status()).toBe(404);
  expect((await page.request.get("/assets/missing.js")).status()).toBe(404);
});

test("narrow viewport resizes the chart and keeps page-level scrolling in bounds", async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto("/");
  await expect(page.getByRole("img",{name:"六线净值曲线"}).locator("canvas").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  const screenshots = process.env.PHASE7B_SCREENSHOTS;
  if (screenshots) {
    await mkdir(screenshots,{recursive:true});
    await page.screenshot({path:join(screenshots,"dashboard-mobile.png"),fullPage:true,animations:"disabled"});
  }
  await page.setViewportSize({width:700,height:850});
  await expect.poll(async () => page.getByRole("img",{name:"六线净值曲线"}).locator("canvas").first()
    .evaluate((canvas) => canvas.getBoundingClientRect().width)).toBeGreaterThan(500);
  await page.getByRole("link",{name:"持仓与成交",exact:true}).click();
  await expect(page.getByRole("heading",{name:"持仓、成交与分红审计"})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(700);
});
