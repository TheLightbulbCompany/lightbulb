import test from 'node:test';
import assert from 'node:assert/strict';
import { createCipheriv, randomBytes } from 'node:crypto';
import { ownerKey } from './envelope.mjs';

// Sealed exactly like integration_secret_store._seal: AESGCM(key).encrypt(nonce, payload, aad) = ciphertext || tag.
function seal(dataKey, ownerId, provider, payload) {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', dataKey, nonce);
  cipher.setAAD(Buffer.from(`${ownerId}\0${provider}`));
  const body = Buffer.concat([cipher.update(JSON.stringify(payload)), cipher.final(), cipher.getAuthTag()]);
  return { data_key: { S: 'd3JhcHBlZA==' }, nonce: { S: nonce.toString('base64') }, ciphertext: { S: body.toString('base64') } };
}

test('opens the envelope, else reads the legacy secret', async () => {
  const dataKey = randomBytes(32);
  const items = { 'org_a|openrouter/h1': seal(dataKey, 'org_a', 'openrouter/h1', { key: 'sk-or-new' }) };
  const contexts = [];
  const deps = {
    env: { CREDENTIAL_ENVELOPES_TABLE: 'credential-envelopes' },
    ddb: { async getItem(p) { return { Item: items[`${p.Key.owner_id.S}|${p.Key.provider.S}`] }; } },
    kms: { async decrypt(p) { contexts.push(p.EncryptionContext); return { Plaintext: dataKey }; } },
    secrets: { async getSecretValue(p) { return { SecretString: `legacy:${p.SecretId}` }; } },
  };
  assert.equal(await ownerKey(deps, 'org_a', 'h1', 'old'), 'sk-or-new');
  assert.deepEqual(contexts, [{ owner_id: 'org_a', provider: 'openrouter/h1' }]);
  delete items['org_a|openrouter/h1'];
  assert.equal(await ownerKey(deps, 'org_a', 'h1', 'old'), 'legacy:old');
});

// Fixed vector from the backend's integration_secret_store._seal (data key bytes 0..31, nonce 0..11);
// tests/credentials/test_credential_envelope_store.py pins the same bytes.
test('decrypts the backend _seal vector', async () => {
  const hex = h => ({ S: Buffer.from(h, 'hex').toString('base64') });
  const item = {
    data_key: { S: 'd3JhcHBlZA==' },
    nonce: hex('000102030405060708090a0b'),
    ciphertext: hex('3c20bd7ebcc7f83baf32fca6de9b551be6b5f35b82592267ce4000954ed420ecf3126300b422e3'),
  };
  const deps = {
    env: { CREDENTIAL_ENVELOPES_TABLE: 't' },
    ddb: { async getItem() { return { Item: item }; } },
    kms: { async decrypt() { return { Plaintext: Buffer.from([...Array(32).keys()]) }; } },
    secrets: {},
  };
  assert.equal(await ownerKey(deps, 'org_v', 'hv', 'unused'), 'sk-or-vector');
});
