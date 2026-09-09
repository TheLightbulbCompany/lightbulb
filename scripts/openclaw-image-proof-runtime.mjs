#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";

const OPENCLAW = "/app/openclaw.mjs";
const GATEWAY_PORT = 18789;
const GATEWAY_TOKEN = "tok_chaos";
const COMMON_PLUGIN_IDS = [
  "lobster",
  "llm-task",
  "tokenjuice",
  "firecrawl",
  "memory-core",
  "memory-wiki",
  "parallel",
  "acpx",
  "sage-openclaw",
  "lightbulb-marketplace",
  "aws-exec-profile",
  "paperclip-exec-creds",
];
const FIXTURES = [
  { file: "bedrock.json", acpAgent: "claude", bedrock: true },
  { file: "byo-openai.json", acpAgent: "codex", smoke: true },
  { file: "chatgpt-oauth.json", acpAgent: "opencode" },
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function parseJson(output, label) {
  try {
    return JSON.parse(output);
  } catch (error) {
    throw new Error(`${label} did not return JSON: ${error.message}`);
  }
}

function runOpenClaw(args, env, label) {
  const result = spawnSync(process.execPath, [OPENCLAW, ...args], {
    encoding: "utf8",
    env,
    maxBuffer: 32 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error(
      `${label} failed (${result.status ?? result.signal ?? "unknown"})\n${result.stderr || result.stdout}`,
    );
  }
  return parseJson(result.stdout, label);
}

function configuredTools(report) {
  return new Set((report?.tools ?? []).flatMap((entry) => entry.names ?? []));
}

function configuredCapability(report, kind) {
  return new Set(report?.capabilities?.find((entry) => entry.kind === kind)?.ids ?? []);
}

function assertConfig(config, fixture) {
  assert(config.meta?.lastTouchedVersion === "2026.9.3", `${fixture.file}: wrong config version`);
  assert(
    config.channels && Object.keys(config.channels).length === 0,
    `${fixture.file}: channel fixtures must remain empty and conditional`,
  );
  assert(config.acp?.enabled === true, `${fixture.file}: production ACP default is missing`);
  assert(
    config.acp?.allowedAgents?.includes(fixture.acpAgent),
    `${fixture.file}: expected ACP agent ${fixture.acpAgent}`,
  );
  const entries = config.plugins?.entries ?? {};
  for (const id of COMMON_PLUGIN_IDS) {
    assert(entries[id]?.enabled === true, `${fixture.file}: ${id} is not enabled in generated config`);
  }
  if (fixture.bedrock) {
    assert(
      entries["amazon-bedrock"]?.config?.discovery?.enabled === false,
      `${fixture.file}: Bedrock discovery must remain explicitly disabled`,
    );
    assert(
      config.models?.providers?.["amazon-bedrock"],
      `${fixture.file}: generated Bedrock provider catalog is missing`,
    );
  }
}

function assertRuntimeReports(reports, fixture) {
  assert(Array.isArray(reports), `${fixture.file}: plugin runtime inventory is malformed`);
  const byId = new Map(reports.map((report) => [report.plugin?.id, report]));
  for (const id of [...COMMON_PLUGIN_IDS, ...(fixture.bedrock ? ["amazon-bedrock"] : [])]) {
    assert(byId.get(id)?.plugin?.status === "loaded", `${fixture.file}: ${id} did not load`);
  }

  const requireTools = (id, names) => {
    const tools = configuredTools(byId.get(id));
    for (const name of names) assert(tools.has(name), `${fixture.file}: ${id} missing tool ${name}`);
  };
  requireTools("lobster", ["lobster"]);
  requireTools("llm-task", ["llm-task"]);
  requireTools("firecrawl", ["firecrawl_search", "firecrawl_scrape"]);
  requireTools("memory-core", ["intent", "memory_get", "memory_search"]);
  requireTools("memory-wiki", ["wiki_apply", "wiki_get", "wiki_lint", "wiki_search", "wiki_status"]);
  requireTools("lightbulb-marketplace", ["marketplace_search", "recommend_agents"]);

  const hooks = (id) => new Set((byId.get(id)?.typedHooks ?? []).map((hook) => hook.name));
  assert(hooks("sage-openclaw").has("before_tool_call"), `${fixture.file}: Sage hook is missing`);
  for (const id of ["aws-exec-profile", "paperclip-exec-creds"]) {
    assert(hooks(id).has("resolve_exec_env"), `${fixture.file}: ${id} credential hook is missing`);
  }
  assert(hooks("acpx").has("reply_dispatch"), `${fixture.file}: ACPX reply dispatch is missing`);
  assert(
    byId.get("acpx")?.services?.includes("acpx-runtime"),
    `${fixture.file}: ACPX runtime service is missing`,
  );
  assert(
    configuredCapability(byId.get("acpx"), "session-catalog").has("pi"),
    `${fixture.file}: ACPX Pi session catalog is missing`,
  );
  assert(
    configuredCapability(byId.get("firecrawl"), "web-fetch").has("firecrawl"),
    `${fixture.file}: Firecrawl fetch provider is missing`,
  );
  for (const id of ["firecrawl", "firecrawl-free"]) {
    assert(
      configuredCapability(byId.get("firecrawl"), "web-search").has(id),
      `${fixture.file}: Firecrawl search provider ${id} is missing`,
    );
  }
  for (const id of ["parallel", "parallel-free"]) {
    assert(
      configuredCapability(byId.get("parallel"), "web-search").has(id),
      `${fixture.file}: Parallel search provider ${id} is missing`,
    );
  }
  assert(
    JSON.stringify(byId.get("tokenjuice")?.plugin?.contracts?.agentToolResultMiddleware) ===
      JSON.stringify(["openclaw", "codex"]),
    `${fixture.file}: tokenjuice runtime contract is missing`,
  );
  if (fixture.bedrock) {
    assert(
      configuredCapability(byId.get("amazon-bedrock"), "text-inference").has("amazon-bedrock"),
      `${fixture.file}: Bedrock inference provider is missing`,
    );
    assert(
      configuredCapability(byId.get("amazon-bedrock"), "embedding").has("bedrock"),
      `${fixture.file}: Bedrock embedding provider is missing`,
    );
  }
}

function writeOpenAiResponse(response) {
  const text = '{"qualified":true}';
  const message = {
    type: "message",
    id: "qualification-message",
    role: "assistant",
    status: "completed",
    content: [{ type: "output_text", text, annotations: [] }],
  };
  const events = [
    {
      type: "response.output_item.added",
      output_index: 0,
      item: { ...message, status: "in_progress", content: [] },
    },
    {
      type: "response.output_text.delta",
      item_id: message.id,
      output_index: 0,
      content_index: 0,
      delta: text,
    },
    {
      type: "response.output_text.done",
      item_id: message.id,
      output_index: 0,
      content_index: 0,
      text,
    },
    { type: "response.output_item.done", output_index: 0, item: message },
    {
      type: "response.completed",
      response: {
        id: "qualification-response",
        status: "completed",
        output: [message],
        usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
      },
    },
  ];
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-store",
    connection: "keep-alive",
  });
  response.end(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`);
}

async function startInferenceStub() {
  let inferenceCalls = 0;
  let sawSentinel = false;
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (request.method === "GET" && url.pathname === "/v1/models") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ object: "list", data: [{ id: "gpt-5.4-mini", object: "model" }] }));
      return;
    }
    if (request.method !== "POST" || url.pathname !== "/v1/responses") {
      response.writeHead(404).end();
      return;
    }
    let body = "";
    for await (const chunk of request) body += String(chunk);
    inferenceCalls += 1;
    sawSentinel ||= body.includes("qualification-sentinel");
    writeOpenAiResponse(response);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address !== "string", "inference stub did not bind");
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    assertCalled() {
      assert(inferenceCalls === 1, `expected one stub inference call, saw ${inferenceCalls}`);
      assert(sawSentinel, "stub inference did not receive the qualification prompt");
    },
    async close() {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

async function waitForGateway(child, stderr) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`gateway exited before readiness\n${stderr()}`);
    try {
      const response = await fetch(`http://127.0.0.1:${GATEWAY_PORT}/healthz`);
      if (response.ok) return;
    } catch {
      // Gateway is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`gateway readiness timed out\n${stderr()}`);
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.kill("SIGTERM");
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000))]);
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
}

function hasQualifiedResult(value) {
  if (!value || typeof value !== "object") return false;
  if (value.qualified === true) return true;
  return Object.values(value).some(hasQualifiedResult);
}

async function runDispatchSmoke(configPath, baseEnv) {
  const inference = await startInferenceStub();
  const gatewayEnv = {
    ...baseEnv,
    OPENAI_API_KEY: "qualification-openai-key",
    OPENAI_BASE_URL: inference.baseUrl,
    OPENCLAW_URL: `http://127.0.0.1:${GATEWAY_PORT}`,
    OPENCLAW_TOKEN: GATEWAY_TOKEN,
  };
  const gateway = spawn(process.execPath, [OPENCLAW, "gateway", "--port", String(GATEWAY_PORT)], {
    env: gatewayEnv,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let gatewayStderr = "";
  gateway.stderr.on("data", (chunk) => {
    gatewayStderr = `${gatewayStderr}${chunk}`.slice(-64 * 1024);
  });
  gateway.stdout.on("data", () => {});
  try {
    await waitForGateway(gateway, () => gatewayStderr);
    const args = {
      action: "run",
      pipeline:
        "openclaw.invoke --tool llm-task --action json --args-json '" +
        '{"prompt":"Return qualified=true for qualification-sentinel",' +
        '"schema":{"type":"object","properties":{"qualified":{"const":true}},' +
        '"required":["qualified"],"additionalProperties":false}}' +
        "'",
      timeoutMs: 45_000,
    };
    const response = await fetch(`http://127.0.0.1:${GATEWAY_PORT}/tools/invoke`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${GATEWAY_TOKEN}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ tool: "lobster", args }),
      signal: AbortSignal.timeout(60_000),
    });
    const body = parseJson(await response.text(), "Lobster dispatch");
    assert(response.ok && body.ok === true, `Lobster dispatch failed: ${JSON.stringify(body)}`);
    assert(body.result?.details?.status === "ok", "Lobster workflow did not complete");
    assert(hasQualifiedResult(body.result?.details?.output), "LLM Task result did not reach Lobster");
    inference.assertCalled();
  } finally {
    await stopChild(gateway);
    await inference.close();
  }
}

async function main() {
  const fixtureDir = process.argv[2];
  assert(fixtureDir, "usage: openclaw-image-proof-runtime.mjs FIXTURE_DIR");
  const qualified = [];
  for (const fixture of FIXTURES) {
    const configPath = path.join(fixtureDir, fixture.file);
    const config = parseJson(await readFile(configPath, "utf8"), fixture.file);
    assertConfig(config, fixture);
    const stateDir = await mkdtemp(path.join(tmpdir(), "openclaw-image-proof-"));
    const env = {
      ...process.env,
      AWS_ACCESS_KEY_ID: "qualification",
      AWS_DEFAULT_REGION: "us-east-1",
      AWS_SECRET_ACCESS_KEY: "qualification",
      OPENAI_API_KEY: "qualification",
      OPENCLAW_CONFIG_PATH: configPath,
      OPENCLAW_HOOK_TOKEN: "qualification-hook",
      OPENCLAW_STATE_DIR: stateDir,
    };
    try {
      const validation = runOpenClaw(["config", "validate", "--json"], env, `${fixture.file} validation`);
      assert(validation.valid === true, `${fixture.file}: config validation failed`);
      const reports = runOpenClaw(
        ["plugins", "inspect", "--all", "--runtime", "--json"],
        env,
        `${fixture.file} runtime inspection`,
      );
      assertRuntimeReports(reports, fixture);
      if (fixture.smoke) await runDispatchSmoke(configPath, env);
      qualified.push(fixture.file);
    } finally {
      await rm(stateDir, { recursive: true, force: true });
    }
  }
  process.stdout.write(`${JSON.stringify({ qualified, dispatch: "lobster -> llm-task -> stub" }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack : String(error));
  process.exitCode = 1;
});
