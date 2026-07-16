// Spike: the ACP/Claude Code shape — InvokeModelWithResponseStream with an
// Anthropic Messages body through the proxy. Proves the invoke eventstream also
// pipes verbatim and that usage is recoverable from `amazon-bedrock-invocationMetrics`.
import {
  BedrockRuntimeClient,
  InvokeModelWithResponseStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";
import { NodeHttpHandler } from "@smithy/node-http-handler";

const PORT = Number(process.env.SPIKE_PORT || 8899);
const AIP = process.env.SPIKE_AIP;
if (!AIP) { console.error("set SPIKE_AIP"); process.exit(2); }

const client = new BedrockRuntimeClient({
  region: process.env.AWS_REGION || "us-east-1",
  endpoint: `http://localhost:${PORT}`,
  credentials: { accessKeyId: "dummy", secretAccessKey: "dummy" },
  requestHandler: new NodeHttpHandler(),
});

const body = {
  anthropic_version: "bedrock-2023-05-31",
  max_tokens: 256,
  messages: [{ role: "user", content: "Say hello in one short sentence." }],
};

let text = "", invocationMetrics = null, chunks = 0;
try {
  const resp = await client.send(new InvokeModelWithResponseStreamCommand({
    modelId: AIP,
    contentType: "application/json",
    accept: "application/json",
    body: JSON.stringify(body),
  }));
  for await (const ev of resp.body) {
    if (ev.chunk?.bytes) {
      chunks++;
      const p = JSON.parse(Buffer.from(ev.chunk.bytes).toString("utf8"));
      if (p.type === "content_block_delta" && p.delta?.text) text += p.delta.text;
      if (p["amazon-bedrock-invocationMetrics"]) invocationMetrics = p["amazon-bedrock-invocationMetrics"];
    }
  }
  console.log(JSON.stringify({ spike: "invoke-client", ok: true, chunks, textLen: text.length, invocationMetrics, textPreview: text.slice(0, 100) }, null, 2));
} catch (e) {
  console.log(JSON.stringify({ spike: "invoke-client", ok: false, error: String(e), name: e.name }, null, 2));
  process.exit(1);
}
