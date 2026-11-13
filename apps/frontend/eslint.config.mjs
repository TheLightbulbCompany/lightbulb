import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import boundaries from "eslint-plugin-boundaries";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Playwright generated files
    "test-results/**",
    // A stale copy of the removed DCV live view's SDK (gitignored), not our source.
    "public/nice-dcv-web-client-sdk/**",
  ]),
  // CEO A2: prevent the public client bundle from importing admin code.
  // The middleware host gate (CEO A1) keeps unauth'd users out at the edge;
  // this rule is the build-time backstop so a stray `import {...} from
  // "@/components/admin/..."` in a public component fails CI instead of
  // shipping admin React to anonymous visitors.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { boundaries },
    settings: {
      "boundaries/elements": [
        {
          type: "admin",
          pattern: ["src/app/admin/**/*", "src/components/admin/**/*"],
          mode: "file",
        },
        {
          type: "public",
          pattern: ["src/**/*"],
          mode: "file",
        },
      ],
      "boundaries/include": ["src/**/*"],
      "boundaries/ignore": ["**/__tests__/**", "tests/**"],
    },
    rules: {
      "boundaries/dependencies": [
        "error",
        {
          default: "disallow",
          rules: [
            // Admin code can import anywhere in the app.
            { from: { type: "admin" }, allow: { to: { type: ["admin", "public"] } } },
            // Public code may only import other public code — admin is off-limits.
            { from: { type: "public" }, allow: { to: { type: "public" } } },
          ],
        },
      ],
    },
  },
]);

export default eslintConfig;
