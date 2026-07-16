// Spike client — stands in for OpenClaw's amazon-bedrock provider: an AWS SDK
// BedrockRuntimeClient with a CUSTOM endpoint (our proxy) + dummy creds (the
// proxy re-signs). Sends a cache-heavy, thinking-enabled ConverseStream and
// consumes the stream, proving the piped bytes parse end-to-end and the usage
// the client sees == the usage the proxy teed == the invocation-log record.
import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";
import { NodeHttpHandler } from "@smithy/node-http-handler";

const PORT = Number(process.env.SPIKE_PORT || 8899);
const AIP = process.env.SPIKE_AIP;
if (!AIP) { console.error("set SPIKE_AIP to an inference-profile ARN"); process.exit(2); }

const client = new BedrockRuntimeClient({
  region: process.env.AWS_REGION || "us-east-1",
  endpoint: `http://localhost:${PORT}`,
  credentials: { accessKeyId: "dummy", secretAccessKey: "dummy" }, // proxy ignores inbound auth in the spike
  // Mirror AWS_BEDROCK_FORCE_HTTP1=1: the SDK defaults to HTTP/2, our proxy is
  // HTTP/1.1. OpenClaw sets this flag for the same reason with a custom endpoint.
  requestHandler: new NodeHttpHandler(),
});

const cmd = new ConverseStreamCommand({
  modelId: AIP,
  messages: [{ role: "user", content: [{ text: "In exactly 3 sentences, explain why the sky is blue. Think it through first." }] }],
  system: [{ text: "You are terse." + " padding".repeat(400) }], // pad system so a cache write happens
  additionalModelRequestFields: { thinking: { type: "adaptive", display: "summarized" }, output_config: { effort: "low" } },
});

let text = "", reasoning = "", sawReasoning = false, clientUsage = null, blocks = 0;
try {
  const resp = await client.send(cmd);
  for await (const ev of resp.stream) {
    if (ev.contentBlockStart) blocks++;
    if (ev.contentBlockDelta?.delta?.text) text += ev.contentBlockDelta.delta.text;
    if (ev.contentBlockDelta?.delta?.reasoningContent) { sawReasoning = true; reasoning += ev.contentBlockDelta.delta.reasoningContent.text || ""; }
    if (ev.metadata?.usage) clientUsage = ev.metadata.usage;
  }
  console.log(JSON.stringify({
    spike: "client-result",
    ok: true,
    textLen: text.length,
    sawReasoning,
    reasoningLen: reasoning.length,
    contentBlocks: blocks,
    clientUsage,
    textPreview: text.slice(0, 120),
  }, null, 2));
} catch (e) {
  console.log(JSON.stringify({ spike: "client-result", ok: false, error: String(e), name: e.name }, null, 2));
  process.exit(1);
}
