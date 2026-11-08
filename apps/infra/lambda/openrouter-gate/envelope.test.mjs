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
