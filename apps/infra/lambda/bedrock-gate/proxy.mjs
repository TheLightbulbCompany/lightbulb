// Sign to bedrock-runtime with the GATEWAY role's creds and stream the response
// back VERBATIM while tee-decoding just enough of the eventstream to pull usage.
// The signing/tee mechanics here are exactly what the spike (spike/) proved
// against real Bedrock. Byte-identical passthrough is load-bearing: Claude Code
// hard-fails ("Truncated event message received") if the content-type or framing
// is altered.
import https from "node:https";
import { createHash, createHmac } from "node:crypto";
import { SignatureV4 } from "@smithy/signature-v4";
import { HttpRequest } from "@smithy/protocol-http";
import { fromNodeProviderChain } from "@aws-sdk/credential-providers";
import { EventStreamCodec } from "@smithy/eventstream-codec";
import { toUtf8, fromUtf8 } from "@smithy/util-utf8";

class NodeSha256 {
  constructor(secret) { this.h = secret ? createHmac("sha256", secret) : createHash("sha256"); }
  update(d) { this.h.update(d); }
  async digest() { return new Uint8Array(this.h.digest()); }
}

let _signer = null;
function signer(region) {
  if (!_signer) {
    _signer = new SignatureV4({ service: "bedrock", region, credentials: fromNodeProviderChain(), sha256: NodeSha256 });
  }
  return _signer;
}

const codec = new EventStreamCodec(toUtf8, fromUtf8);

// Greedy eventstream frame decoder — buffers partial TCP chunks and decodes by
// the 4-byte big-endian total-length prefix. Extracts Converse `metadata.usage`
// and Invoke `amazon-bedrock-invocationMetrics`; ignores everything else.
class UsageTee {
  constructor() { this.buf = Buffer.alloc(0); this.usage = null; }
  feed(chunk) {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : Buffer.from(chunk);
    while (this.buf.length >= 4) {
      const total = this.buf.readUInt32BE(0);
      if (this.buf.length < total || total < 16) break;
      const frame = this.buf.subarray(0, total);
      this.buf = this.buf.subarray(total);
      try {
        const msg = codec.decode(frame);
        const evType = msg.headers[":event-type"]?.value;
        if (evType === "metadata") {
          const p = JSON.parse(toUtf8(msg.body));
          if (p.usage) this.usage = p.usage;
        } else if (evType === "chunk") {
          const p = JSON.parse(toUtf8(msg.body));
          if (p["amazon-bedrock-invocationMetrics"]) this.usage = p["amazon-bedrock-invocationMetrics"];
        }
      } catch { /* partial/again */ }
    }
  }
}

/**
 * Forward one request to Bedrock and pipe the response to `write`/`writeHead`
 * verbatim. Returns { status, requestId, usage } once the stream ends.
 * @param {(status:number, headers:object)=>void} writeHead
 * @param {(chunk:Buffer)=>void} write
 * @param {()=>void} end
 * @param {AbortSignal} [abortSignal]
 */
export function forward({ region, path, bodyBuf, writeHead, write, end, abortSignal }) {
  const host = `bedrock-runtime.${region}.amazonaws.com`;
  return new Promise(async (resolve, reject) => {
    let signed;
    try {
      signed = await signer(region).sign(
        new HttpRequest({
          method: "POST", protocol: "https:", hostname: host, path,
          headers: { host, "content-type": "application/json", accept: "application/vnd.amazon.eventstream" },
          body: bodyBuf,
        })
      );
    } catch (e) { return reject(e); }

    const req = https.request({ hostname: host, path, method: "POST", headers: signed.headers, signal: abortSignal }, (up) => {
      writeHead(up.statusCode, up.headers);
      const tee = new UsageTee();
      const requestId = up.headers["x-amzn-requestid"] || up.headers["x-amzn-requestId"];
      up.on("data", (chunk) => {
        write(chunk); // verbatim FIRST, then tee a copy — never gate the pipe on decode
        if (up.statusCode === 200) tee.feed(chunk);
      });
      up.on("end", () => { end(); resolve({ status: up.statusCode, requestId, usage: tee.usage }); });
      up.on("error", reject);
    });
    req.on("error", reject);
    req.end(bodyBuf);
  });
}
