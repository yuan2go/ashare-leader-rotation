import { queryOptions } from "@tanstack/react-query";
import { api } from "./client";
import type { Schema } from "./client";

export const healthQuery = queryOptions({
  queryKey: ["health"],
  queryFn: ({signal}) => api.health(signal),
  refetchInterval: 60_000,
});

export const overviewQuery = queryOptions({
  queryKey: ["shadow", "overview"],
  queryFn: ({signal}) => api.overview(signal),
});

export const collectionQuery = queryOptions({
  queryKey: ["ops", "collection"],
  queryFn: ({signal}) => api.collection(signal),
});

export const walletQuery = (benchmark: "E4M" | "DEF50", capital: Schema["Capital"]) =>
  queryOptions({queryKey: ["wallet", benchmark, capital],
    queryFn: ({signal}) => api.wallet({benchmark_account: benchmark, capital_rmb: capital}, signal)});
