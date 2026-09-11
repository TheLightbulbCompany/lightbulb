// Emit the vendored zod ConfigSchema as a JSON Schema on stdout.
//
// WHY: the backend image is Python-only (apps/backend/Dockerfile is
// python:3.12-slim — no Node), so it cannot run the zod bundle to validate a
// merged openclaw.json before persisting it to EFS. This converts the same
// vendored schema into a JSON Schema the `jsonschema` package can enforce from
// Python (core/services/config_schema.py).
//
// Self-contained: the bundle re-exports zod, so this needs `node` and nothing
// else — which is why the CI drift guard (tests/chaos/test_schema_staleness.py)
// can regenerate and diff on a runner with no npm install.
//
// Run via scripts/vendor_openclaw_schema.sh; exit 2 when the bundle is absent,
// matching validate.mjs so callers can tell "not vendored" from "conversion
// failed".
import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const bundlePath = process.argv[2]
  ? resolve(process.argv[2])
  : join(here, "openclaw-config-schema.mjs");

if (!existsSync(bundlePath)) {
  console.error("schema bundle missing: run scripts/vendor_openclaw_schema.sh");
  process.exit(2);
}

const { ConfigSchema, z } = await import(pathToFileURL(bundlePath).href);

// io:"input" — we validate what is WRITTEN to disk, before zod defaults/
// transforms are applied, which is exactly what OpenClaw's loader parses.
// unrepresentable:"any" — a handful of leaves (functions, custom refinements)
// have no JSON Schema form; widening them to `any` keeps the export loose where
// it cannot be faithful rather than failing the whole conversion.
const jsonSchema = z.toJSONSchema(ConfigSchema, {
  io: "input",
  unrepresentable: "any",
  cycles: "ref",
  reused: "ref",
});

process.stdout.write(JSON.stringify(jsonSchema, null, 2) + "\n");
