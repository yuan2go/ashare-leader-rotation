import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { transferableAbortController } from "node:util";
import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => {
  // Node's native Request requires an AbortSignal from the same realm.
  vi.stubGlobal("AbortController", transferableAbortController().constructor);
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("Use an explicit test transport"); }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
