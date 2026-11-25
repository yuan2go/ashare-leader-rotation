import { describe, expect, it, vi } from "vitest";
import { api } from "../src/api/client";
import { cents, changeClass, pct, pp, rmb } from "../src/utils/format";

function stubFetch(response: Response) {
  const fetch = vi.fn<(request: Request) => Promise<Response>>().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("OpenAPI client", () => {
  it("serializes the contract capital, account and dates", async () => {
    const fetch = stubFetch(Response.json([]));
    await expect(api.nav({capital_rmb: "2000000", account: "V5", start: "2026-11-24"})).resolves.toEqual([]);
    const url = new URL(fetch.mock.calls[0][0].url);
    expect(url.pathname).toBe("/api/v1/shadow/nav");
    expect(Object.fromEntries(url.searchParams)).toEqual({capital_rmb: "2000000", account: "V5", start: "2026-11-24"});
  });

  it("reads signed API errors and retains compatibility with the brief's alternate shape", async () => {
    stubFetch(Response.json({code: "job_busy", message: "已有任务"}, {status: 409}));
    await expect(api.jobs()).rejects.toMatchObject({status: 409, code: "job_busy", message: "已有任务"});
    stubFetch(Response.json({error_code: "invalid_day", message: "日期无效", detail: {field: "day"}}, {status: 422}));
    await expect(api.jobs()).rejects.toMatchObject({status: 422, code: "invalid_day", detail: {field: "day"}});
  });

  it("posts the typed action and operations header without permitting market requests by default", async () => {
    const fetch = stubFetch(Response.json({state: "queued"}, {status: 202}));
    await api.submitJob({action: "VerifyOffline", allow_market_http: false});
    const request = fetch.mock.calls[0][0];
    expect(request.method).toBe("POST");
    expect(request.headers.get("X-Shadow-Ops")).toBe("1");
    await expect(request.json()).resolves.toEqual({action: "VerifyOffline", allow_market_http: false});
  });

  it("returns original attachment bytes", async () => {
    const csv = "account\u000aV5\u000a";
    const fetch = stubFetch(new Response(csv, {headers: {"Content-Type": "text/csv"}}));
    const file = await api.ticketFile("csv");
    const contents = await new Promise<string | ArrayBuffer | null>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(file);
    });
    expect(contents).toBe(csv);
    expect(new URL(fetch.mock.calls[0][0].url).pathname).toContain("next-open.csv");
  });
});

describe("display units", () => {
  it("formats cents, percentages and points without changing supplied values", () => {
    expect(cents(1234567)).toBe("¥12,345.67");
    expect(rmb(2.781, 3)).toBe("¥2.781");
    expect(pct(-1.235, true)).toBe("-1.24%");
    expect(pp(2.5, true)).toBe("+2.50 pp");
    expect(pct(null)).toBe("—");
    expect(pp(0, true)).toBe("0.00 pp");
    expect(changeClass(1)).toBe("market-up");
    expect(changeClass(-1)).toBe("market-down");
  });
});
