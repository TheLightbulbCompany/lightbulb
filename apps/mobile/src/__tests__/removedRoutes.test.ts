// `import ... from "node:fs"` fails tsc here (this project has no @types/node —
// see tsconfig.json's `"types": ["jest"]`). A `require()` call isn't
// type-resolved, so it is fine; `__dirname` just needs a local declaration.
declare const __dirname: string;
const { readdirSync, readFileSync, statSync } = require("node:fs");
const { join } = require("node:path");

const SRC = join(__dirname, "..");
// The old chat app's screens: nothing may link one again.
const REMOVED = [/\(tabs\)\/home\b/, /\(tabs\)\/dms\b/, /\(tabs\)\/more\b/, /\/\(app\)\/sessions?\b/, /"\/\(app\)\/teams"/,
  /"\/\(app\)\/settings"/, /\/\(app\)\/(room\/[^"`]*\/details|task|automation|control|work|workspaces|library|invite)\b/, /"\/agent\//, /"\/(workspaces|invite)"/];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name: string) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

it("links no route that the voice app removed", () => {
  const hits = sources(SRC).flatMap((file) =>
    REMOVED.filter((pattern) => pattern.test(readFileSync(file, "utf8"))).map((pattern) => `${file}: ${pattern}`),
  );
  expect(hits).toEqual([]);
});
