// `import ... from "node:fs"` fails tsc here (this project has no @types/node —
// see tsconfig.json's `"types": ["jest"]`, deliberately not touched for one
// test file). A `require()` call, unlike an `import`, isn't type-resolved and
// so is fine as an implicit `any` under this repo's tsconfig; `__dirname` just
// needs a local ambient declaration.
declare const __dirname: string;
const { readdirSync, readFileSync, statSync } = require("node:fs");
const { join } = require("node:path");

const SRC = join(__dirname, "..");
const REMOVED = [/\(tabs\)\/home\b/, /\(tabs\)\/dms\b/, /\(tabs\)\/more\b/, /\/\(app\)\/sessions\b/, /"\/\(app\)\/teams"/, /"\/\(app\)\/settings"/];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name: string) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

it("links no route that the redesign removed", () => {
  const hits = sources(SRC).flatMap((file) =>
    REMOVED.filter((pattern) => pattern.test(readFileSync(file, "utf8"))).map((pattern) => `${file}: ${pattern}`),
  );
  expect(hits).toEqual([]);
});

// A session is a row in the Chats list, and the row is what links to it.
it("keeps sessions reachable from Chats", () => {
  expect(readFileSync(join(SRC, "app/(app)/(tabs)/chats.tsx"), "utf8")).toContain("<SessionChatRow");
  expect(readFileSync(join(SRC, "components/chats/ChatRow.tsx"), "utf8")).toContain("/(app)/session/");
});
