// Tiny DynamoDB helpers — the gate touches a handful of attributes on a few
// tables, so hand-marshalling keeps the Lambda dependency-light and the tests
// trivial (inject a fake with getItem/updateItem/putItem). All functions take
// the low-level client so tests pass a mock; production wires the real
// DynamoDBClient in handler.mjs.

export const S = (v) => (v == null ? undefined : { S: String(v) });
export const N = (v) => ({ N: String(v) });
export const BOOL = (v) => ({ BOOL: !!v });

// Read a scalar out of a raw AttributeValue.
export const readS = (av) => (av && av.S !== undefined ? av.S : undefined);
export const readN = (av) => (av && av.N !== undefined ? Number(av.N) : undefined);
export const readBool = (av) => (av && av.BOOL !== undefined ? av.BOOL : undefined);
// Read a String->String or String->N map into a plain object.
export const readMapS = (av) => {
  if (!av || !av.M) return {};
  const out = {};
  for (const [k, v] of Object.entries(av.M)) out[k] = v.S !== undefined ? v.S : v;
  return out;
};
export const readMapN = (av) => {
  if (!av || !av.M) return {};
  const out = {};
  for (const [k, v] of Object.entries(av.M)) out[k] = v.N !== undefined ? Number(v.N) : 0;
  return out;
};
