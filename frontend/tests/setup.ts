import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { transferableAbortController } from "node:util";
import { afterEach, beforeEach, vi } from "vitest";

configure({asyncUtilTimeout: 4500});

beforeEach(() => {
  // Node's native Request requires an AbortSignal from the same realm.
  vi.stubGlobal("AbortController", transferableAbortController().constructor);
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("Use an explicit test transport"); }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
