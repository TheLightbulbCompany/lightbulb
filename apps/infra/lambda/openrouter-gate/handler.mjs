import { DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand, TransactWriteItemsCommand } from '@aws-sdk/client-dynamodb';
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { KMSClient, DecryptCommand } from '@aws-sdk/client-kms';
import { handleRequest } from './core.mjs';
import { abortOnClose } from './disconnect.mjs';
import { ownerKey } from './envelope.mjs';

const ddbClient = new DynamoDBClient({});
const secretsClient = new SecretsManagerClient({});
const ddb = {
  getItem: p => ddbClient.send(new GetItemCommand(p)),
  putItem: p => ddbClient.send(new PutItemCommand(p)),
  updateItem: p => ddbClient.send(new UpdateItemCommand(p)),
  transactWriteItems: p => ddbClient.send(new TransactWriteItemsCommand(p)),
};
const kmsClient = new KMSClient({});
const store = {
  ddb, env: process.env,
  kms: { decrypt: p => kmsClient.send(new DecryptCommand(p)) },
  secrets: { getSecretValue: p => secretsClient.send(new GetSecretValueCommand(p)) },
};
const secrets = { ownerKey: (...args) => ownerKey(store, ...args) };

export const handler = awslambda.streamifyResponse(async (event, responseStream) => {
  let stream = responseStream, sent = false;
  const abortSignal = abortOnClose(responseStream);
  const responder = {
    abortSignal,
    headersSent: () => sent,
    writeHead(status, headers) { sent = true; stream = awslambda.HttpResponseStream.from(responseStream, { statusCode: status, headers }); },
    write(chunk) { return stream.write(chunk); },
    waitForDrain() { return new Promise((resolve, reject) => {
      const cleanup = () => { stream.off('drain', drained); abortSignal.removeEventListener('abort', aborted); };
      const drained = () => { cleanup(); resolve(); };
      const aborted = () => { cleanup(); reject(new Error('Client disconnected')); };
      stream.once('drain', drained);
      abortSignal.addEventListener('abort', aborted, { once: true });
      if (abortSignal.aborted) aborted();
    }); },
    end() { stream.end(); },
    error(error) { this.writeHead(error.status, { 'content-type': 'application/json' }); this.write(error.body()); this.end(); },
  };
  await handleRequest(event, responder, { ddb, secrets, env: process.env });
  if (!responseStream.writableEnded) responseStream.end();
});
