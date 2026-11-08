import { createDecipheriv } from 'node:crypto';

// The owner's OpenRouter key: the backend's integration_secret_store envelope `openrouter/<key_hash>`
// (credential-envelopes: AES-256-GCM, data key wrapped by KMS under {owner_id, provider}), else the
// legacy secret the key row names (until migrate_secrets_to_envelopes runs).
export async function ownerKey({ ddb, kms, secrets, env }, ownerId, keyHash, secretName) {
  const provider = `openrouter/${keyHash}`;
  const item = (await ddb.getItem({ TableName: env.CREDENTIAL_ENVELOPES_TABLE, Key: { owner_id: { S: ownerId }, provider: { S: provider } }, ConsistentRead: true }))?.Item;
  if (!item) return (await secrets.getSecretValue({ SecretId: secretName })).SecretString;
  const b64 = name => Buffer.from(item[name].S, 'base64');
  const { Plaintext } = await kms.decrypt({ CiphertextBlob: b64('data_key'), EncryptionContext: { owner_id: ownerId, provider } });
  const sealed = b64('ciphertext');
  const decipher = createDecipheriv('aes-256-gcm', Buffer.from(Plaintext), b64('nonce'));
  decipher.setAAD(Buffer.from(`${ownerId}\0${provider}`));
  decipher.setAuthTag(sealed.subarray(-16));
  return JSON.parse(Buffer.concat([decipher.update(sealed.subarray(0, -16)), decipher.final()]).toString('utf8')).key;
}
