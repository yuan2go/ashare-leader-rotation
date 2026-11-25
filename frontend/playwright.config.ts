import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

const executablePath = process.env.PHASE7B_BROWSER_EXE;
const baseURL = process.env.PHASE7B_BASE_URL ?? "http://127.0.0.1:18742";
const python = fileURLToPath(new URL("../data/fetch_cache/runtimes/batch1-py311/Scripts/python.exe", import.meta.url));

export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "*.spec.ts",
  reporter: "list",
  workers: 1,
  use: {
    baseURL,
    serviceWorkers: "block",
    viewport: {width: 1440, height: 1000},
    launchOptions: {executablePath, args: ["--disable-background-networking"]},
  },
  webServer: process.env.PHASE7B_BASE_URL ? undefined : {
    command: `"${python}" -B -X utf8 frontend/tests/browser/serve_offline.py`,
    cwd: "..",
    url: `${baseURL}/api/v1/health`,
    timeout: 45_000,
    reuseExistingServer: false,
  },
});
