import { defineConfig } from "@playwright/test";
export default defineConfig({ testDir: "tasks", outputDir: "test-results", reporter: "list", fullyParallel: false, workers: 1, use: { channel: "chrome" } }); // the installed Google Chrome, no browser download
