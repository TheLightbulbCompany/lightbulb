import { defineConfig } from "vitest/config";
// Integration files share Docker; serve.test counts labelled containers, so run them one file at a time.
export default defineConfig({ test: { include: ["src/**/*.test.ts"], passWithNoTests: true, fileParallelism: !process.env.EVAL_IT } });
