// Throwaway spike server — proves the load-bearing gateway mechanic:
//   receive a Bedrock request → SigV4-sign to bedrock-runtime with OUR creds →
//   pipe the binary eventstream response bytes back VERBATIM → while tee-decoding
//   just enough to pull the final usage event.
// NOT the production handler; no auth, no budget, no clamp — just the passthrough
// + usage-tee proof (spec V3). Run: node server.mjs   (needs AWS creds in env/profile)
import http from "node:http";
import https from "node:https";
import { SignatureV4 } from "@smithy/signature-v4";
import { HttpRequest } from "@smithy/protocol-http";
import { fromNodeProviderChain } from "@aws-sdk/credential-providers";
import { EventStreamCodec } from "@smithy/eventstream-codec";
import { toUtf8, fromUtf8 } from "@smithy/util-utf8";
import { Hash } from "node:crypto";
import { createHash, createHmac } from "node:crypto";

const REGION = process.env.AWS_REGION || "us-east-1";
const PORT = Number(process.env.SPIKE_PORT || 8899);
const BEDROCK_HOST = `bedrock-runtime.${REGION}.amazonaws.com`;

// Minimal Sha256 shim smithy's signer expects.
class NodeSha256 {
  constructor(secret) { this.h = secret ? createHmac("sha256", secret) : createHash("sha256"); }
  update(d) { this.h.update(d); }
  async digest() { return new Uint8Array(this.h.digest()); }
}

const signer = new SignatureV4({
  service: "bedrock",
  region: REGION,
  credentials: fromNodeProviderChain(),
  sha256: NodeSha256,
});

const codec = new EventStreamCodec(toUtf8, fromUtf8);

const server = http.createServer(async (clientReq, clientRes) => {
  const chunks = [];
  for await (const c of clientReq) chunks.push(c);
  const bodyBuf = Buffer.concat(chunks);

  // path is /model/{modelId}/converse-stream ; modelId is URL-encoded (AIP ARN)
  const path = clientReq.url;

  const signed = await signer.sign(new HttpRequest({
    method: "POST",
    protocol: "https:",
    hostname: BEDROCK_HOST,
    path,
    headers: {
      host: BEDROCK_HOST,
      "content-type": "application/json",
      accept: "application/vnd.amazon.eventstream",
    },
    body: bodyBuf,
  }));

  const upstream = https.request(
    { hostname: BEDROCK_HOST, path, method: "POST", headers: signed.headers },
    (upRes) => {
      // Mirror status + headers VERBATIM — content-type is load-bearing for ACP.
      clientRes.writeHead(upRes.statusCode, upRes.headers);

      // Tee: decode eventstream frames as bytes flow, WITHOUT altering the pipe.
      let buffered = Buffer.alloc(0);
      let lastUsage = null;
      let eventCount = 0;
      const seenTypes = new Set();

      upRes.on("data", (chunk) => {
        clientRes.write(chunk); // verbatim passthrough — client gets untouched bytes
        // tee-decode a COPY
        buffered = Buffer.concat([buffered, chunk]);
        // EventStreamCodec needs whole messages; decode greedily what we can.
        while (buffered.length >= 4) {
          const totalLen = buffered.readUInt32BE(0);
          if (buffered.length < totalLen) break;
          const frame = buffered.subarray(0, totalLen);
          buffered = buffered.subarray(totalLen);
          try {
            const msg = codec.decode(frame);
            eventCount++;
            const evType = msg.headers[":event-type"]?.value;
            if (evType) seenTypes.add(evType);
            if (evType === "metadata") {
              const payload = JSON.parse(toUtf8(msg.body));
              if (payload.usage) lastUsage = payload.usage;
            }
          } catch (e) { /* partial/again */ }
        }
      });
      upRes.on("end", () => {
        clientRes.end();
        console.error(JSON.stringify({
          spike: "tee-result",
          status: upRes.statusCode,
          contentType: upRes.headers["content-type"],
          requestId: upRes.headers["x-amzn-requestid"],
          events: eventCount,
          eventTypes: [...seenTypes],
          usage: lastUsage,
        }));
      });
    }
  );
  upstream.on("error", (e) => { clientRes.writeHead(502); clientRes.end(String(e)); });
  upstream.end(bodyBuf);
});

server.listen(PORT, () => console.error(`spike proxy on :${PORT} → ${BEDROCK_HOST}`));
