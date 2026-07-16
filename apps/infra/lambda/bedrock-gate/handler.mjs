// Lambda entry point: wraps core.handleRequest with awslambda.streamifyResponse
// (RESPONSE_STREAM Function URL) and the real DynamoDB client. All logic and
// every seam is in core.mjs / the modules it imports, which unit-test without
// the awslambda global.
import {
  DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand, TransactWriteItemsCommand,
} from "@aws-sdk/client-dynamodb";
import { handleRequest } from "./core.mjs";

const client = new DynamoDBClient({});
// Thin promise-returning facade so modules stay SDK-command-free and testable
// (unit tests inject a fake with the same methods).
const realDdb = {
  getItem: (p) => client.send(new GetItemCommand(p)),
  putItem: (p) => client.send(new PutItemCommand(p)),
  updateItem: (p) => client.send(new UpdateItemCommand(p)),
  transactWriteItems: (p) => client.send(new TransactWriteItemsCommand(p)),
};

const ENV = {
  AWS_REGION: process.env.AWS_REGION || "us-east-1",
  GATE_ENV: process.env.GATE_ENV,
  TOKENS_TABLE: process.env.TOKENS_TABLE,
  BILLING_TABLE: process.env.BILLING_TABLE,
  FAST_GATE_TABLE: process.env.FAST_GATE_TABLE,
  INVOCATION_DEDUP_TABLE: process.env.INVOCATION_DEDUP_TABLE,
  CAP_MICROCENTS: process.env.CAP_MICROCENTS,
  RESERVE_ESTIMATE_MICROCENTS: process.env.RESERVE_ESTIMATE_MICROCENTS,
  DEDUP_TTL_SECONDS: process.env.DEDUP_TTL_SECONDS,
};

// awslambda is a runtime global in the Node Lambda environment.
export const handler = awslambda.streamifyResponse(async (event, responseStream, _context) => {
  let sent = false;
  let http = responseStream;
  const responder = {
    headersSent: () => sent,
    writeHead: (status, headers) => {
      sent = true;
      http = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: status,
        headers: pickHeaders(headers),
      });
    },
    write: (chunk) => http.write(chunk),
    end: () => http.end(),
    error: (gateErr) => {
      sent = true;
      const s = awslambda.HttpResponseStream.from(responseStream, { statusCode: gateErr.status, headers: gateErr.headers() });
      s.write(gateErr.body());
      s.end();
    },
    abortSignal: undefined,
  };
  try {
    await handleRequest(event, responder, { ddb: realDdb, env: ENV });
  } finally {
    if (!responseStream.writableEnded) { try { responseStream.end(); } catch { /* already ended */ } }
  }
});

// Only forward the response headers Bedrock's client needs; content-type is
// load-bearing (ACP). Drop hop-by-hop/transfer headers the Function URL manages.
function pickHeaders(h = {}) {
  const out = {};
  for (const k of ["content-type", "x-amzn-requestid", "x-amzn-bedrock-content-type"]) {
    if (h[k] != null) out[k] = h[k];
  }
  return out;
}
