import createClient from "openapi-fetch";
import type { components, operations, paths } from "./schema";

export type Schema = components["schemas"];
export type QueryOf<Id extends keyof operations> = NonNullable<operations[Id]["parameters"]["query"]>;

const http = createClient<paths>({
  baseUrl: window.location.origin,
  fetch: (request) => globalThis.fetch(request),
});

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  static from(status: number, body: unknown) {
    if (body && typeof body === "object") {
      const fields = body as Partial<Schema["Error"]> & {error_code?: unknown; detail?: unknown};
      const code = typeof fields.code === "string" ? fields.code
        : typeof fields.error_code === "string" ? fields.error_code : "http_error";
      const message = typeof fields.message === "string" ? fields.message
        : typeof fields.detail === "string" ? fields.detail : `请求失败（HTTP ${status}）`;
      return new ApiError(status, code, message, fields.detail);
    }
    return new ApiError(status, "http_error", `请求失败（HTTP ${status}）`);
  }
}

export function errorMessage(error: Error) {
  return error instanceof ApiError ? error.message : "无法连接本地 API，请检查服务后重试。";
}

async function read<T>(request: Promise<{data?: T; error?: unknown; response: Response}>): Promise<T> {
  const {data, error, response} = await request;
  if (!response.ok || data === undefined) throw ApiError.from(response.status, error);
  return data;
}

export const api = {
  health: (signal?: AbortSignal) => read(http.GET("/api/v1/health", {signal})),
  integrity: (signal?: AbortSignal) => read(http.GET("/api/v1/health/integrity", {signal})),
  overview: (signal?: AbortSignal) => read(http.GET("/api/v1/shadow/overview", {signal})),
  risk: (signal?: AbortSignal) => read(http.GET("/api/v1/shadow/risk", {signal})),
  nav: (query: QueryOf<"getShadowNav">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/shadow/nav", {params: {query}, signal})),
  holdings: (query: QueryOf<"getShadowHoldings">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/shadow/holdings", {params: {query}, signal})),
  fills: (query: QueryOf<"getShadowFills">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/shadow/fills", {params: {query}, signal})),
  corporateActions: (query: QueryOf<"getShadowCorporateActions">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/shadow/corporate-actions", {params: {query}, signal})),
  tickets: (query: QueryOf<"getNextOpenTickets">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/tickets/next-open", {params: {query}, signal})),
  ticketFile: (format: "csv" | "md") => read(http.GET(
    format === "csv" ? "/api/v1/tickets/next-open.csv" : "/api/v1/tickets/next-open.md",
    {parseAs: "blob"},
  )),
  portfolios: (query: QueryOf<"getResearchPortfolios">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/research/portfolios", {params: {query}, signal})),
  yearly: (query: QueryOf<"getResearchYearlyReturns">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/research/yearly-returns", {params: {query}, signal})),
  drawdowns: (query: QueryOf<"getResearchDrawdowns">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/research/drawdowns", {params: {query}, signal})),
  factors: (query: QueryOf<"getResearchFactorCourt">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/research/factor-court", {params: {query}, signal})),
  placebos: (query: QueryOf<"getResearchPlacebos">, signal?: AbortSignal) =>
    read(http.GET("/api/v1/research/placebos", {params: {query}, signal})),
  coverage: (signal?: AbortSignal) => read(http.GET("/api/v1/research/coverage", {signal})),
  collection: (signal?: AbortSignal) => read(http.GET("/api/v1/ops/collection-status", {signal})),
  jobs: (signal?: AbortSignal) => read(http.GET("/api/v1/ops/jobs", {signal})),
  submitJob: (body: Schema["JobRequest"]) => read(http.POST("/api/v1/ops/jobs", {
    params: {header: {"x-shadow-ops": "1"}},
    body,
  })),
  job: (job_id: string, signal?: AbortSignal) =>
    read(http.GET("/api/v1/ops/jobs/{job_id}", {params: {path: {job_id}}, signal})),
  jobLog: (job_id: string, signal?: AbortSignal) =>
    read(http.GET("/api/v1/ops/jobs/{job_id}/logs", {params: {path: {job_id}}, signal})),
};
