// A minimal in-memory DynamoDB fake for tests — enough of the semantics the
// ledger relies on: attribute_not_exists / attribute equality conditions,
// SET with if_not_exists + arithmetic, ADD-like nested map math, and
// ConditionalCheckFailedException on a failed condition. NOT a general engine;
// it implements exactly the expressions ledger.mjs / auth.mjs use.
export function makeFakeDdb(seed = {}) {
  // store: { [table]: { [ownerId|hash]: rawItem } }
  const store = JSON.parse(JSON.stringify(seed));
  const keyOf = (Key) => Object.values(Key)[0].S;

  function fail() {
    const e = new Error("The conditional request failed");
    e.name = "ConditionalCheckFailedException";
    throw e;
  }

  function evalCondition(item, cond, names = {}, values = {}) {
    if (!cond) return true;
    // Support the small grammar our code emits.
    const resolvePath = (p) => {
      // e.g. reserved_microcents.#d  -> reserved_microcents.<date>
      const parts = p.split(".").map((seg) => (seg.startsWith("#") ? names[seg] : seg));
      let node = item;
      for (const seg of parts) { if (node && node.M) node = node.M[seg]; else node = node?.[seg]; }
      return { parts, node };
    };
    // OR / AND splits (left-assoc, our conditions are simple)
    if (cond.includes(" OR ")) return cond.split(" OR ").some((c) => evalCondition(item, c.trim(), names, values));
    if (cond.includes(" AND ")) return cond.split(" AND ").every((c) => evalCondition(item, c.trim(), names, values));
    let m;
    if ((m = /^attribute_not_exists\((.+)\)$/.exec(cond))) return resolvePath(m[1]).node === undefined;
    if ((m = /^attribute_exists\((.+)\)$/.exec(cond))) return resolvePath(m[1]).node !== undefined;
    if ((m = /^(.+?)\s*=\s*(:\w+)$/.exec(cond))) {
      const { node } = resolvePath(m[1].trim());
      const want = values[m[2]];
      return node !== undefined && Number(node.N) === Number(want.N);
    }
    throw new Error("fake-ddb: unsupported condition: " + cond);
  }

  function applyUpdate(item, expr, names = {}, values = {}) {
    // Only SET clauses (our ADD is expressed as SET x = if_not_exists(x,:z) +/- :v).
    const setBody = expr.replace(/^SET\s+/i, "");
    for (const clause of splitTopLevel(setBody)) {
      const [lhs, rhs] = clause.split("=").map((s) => s.trim());
      const parts = lhs.split(".").map((seg) => (seg.startsWith("#") ? names[seg] : seg));
      // Evaluate rhs: either :val, {M:{}}, or if_not_exists(path,:z) +/- :v
      let val;
      let mm;
      if (/^:\w+$/.test(rhs)) {
        val = values[rhs];
      } else if ((mm = /^if_not_exists\((.+?),\s*(:\w+)\)\s*([+\-])\s*(:\w+)$/.exec(rhs))) {
        const cur = getPath(item, mm[1], names);
        const base = cur !== undefined ? Number(cur.N) : Number(values[mm[2]].N);
        const delta = Number(values[mm[4]].N);
        val = { N: String(mm[3] === "+" ? base + delta : base - delta) };
      } else {
        throw new Error("fake-ddb: unsupported update rhs: " + rhs);
      }
      setPath(item, parts, val);
    }
  }
  function getPath(item, p, names) {
    const parts = p.split(".").map((seg) => (seg.startsWith("#") ? names[seg] : seg));
    let node = item;
    for (const seg of parts) node = node?.M ? node.M[seg] : node?.[seg];
    return node;
  }
  function setPath(item, parts, val) {
    let node = item;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      if (!node[seg]) node[seg] = { M: {} };
      if (!node[seg].M) node[seg].M = {};
      node = node[seg].M;
    }
    node[parts[parts.length - 1]] = val;
  }
  function splitTopLevel(s) {
    const out = []; let depth = 0, cur = "";
    for (const ch of s) {
      if (ch === "(") depth++; if (ch === ")") depth--;
      if (ch === "," && depth === 0) { out.push(cur); cur = ""; } else cur += ch;
    }
    if (cur.trim()) out.push(cur);
    return out;
  }

  return {
    _store: store,
    async getItem({ TableName, Key }) {
      const it = store[TableName]?.[keyOf(Key)];
      return it ? { Item: JSON.parse(JSON.stringify(it)) } : {};
    },
    async putItem({ TableName, Item, ConditionExpression }) {
      store[TableName] ||= {};
      const k = keyOf({ x: Object.values(Item)[0] });
      const key = Item.request_id?.S ?? Item.token_hash?.S ?? Item.owner_id?.S;
      const existing = store[TableName][key];
      if (ConditionExpression && !evalCondition(existing || {}, ConditionExpression)) fail();
      store[TableName][key] = JSON.parse(JSON.stringify(Item));
      return {};
    },
    async updateItem({ TableName, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues }) {
      store[TableName] ||= {};
      const k = keyOf(Key);
      const item = store[TableName][k] || { ...Key };
      if (ConditionExpression && !evalCondition(item, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues)) fail();
      applyUpdate(item, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues);
      store[TableName][k] = item;
      return {};
    },
    // Atomic: evaluate ALL conditions first; if any fails, throw
    // TransactionCanceledException with CancellationReasons and mutate nothing;
    // otherwise apply every Put/Update. Mirrors DynamoDB TransactWriteItems for
    // the shapes ledger.mjs emits (a conditional Put + a nested-map Update).
    async transactWriteItems({ TransactItems }) {
      const reasons = [];
      let anyFail = false;
      for (const ti of TransactItems) {
        const op = ti.Put || ti.Update;
        const table = op.TableName;
        const key = ti.Put
          ? (op.Item.request_id?.S ?? op.Item.token_hash?.S ?? op.Item.owner_id?.S)
          : keyOf(op.Key);
        const existing = store[table]?.[key];
        const ok = !op.ConditionExpression || evalCondition(existing || {}, op.ConditionExpression, op.ExpressionAttributeNames, op.ExpressionAttributeValues);
        reasons.push(ok ? { Code: "None" } : { Code: "ConditionalCheckFailed" });
        if (!ok) anyFail = true;
      }
      if (anyFail) {
        const e = new Error("Transaction cancelled");
        e.name = "TransactionCanceledException";
        e.CancellationReasons = reasons;
        throw e;
      }
      for (const ti of TransactItems) {
        if (ti.Put) {
          const p = ti.Put;
          store[p.TableName] ||= {};
          const key = p.Item.request_id?.S ?? p.Item.token_hash?.S ?? p.Item.owner_id?.S;
          store[p.TableName][key] = JSON.parse(JSON.stringify(p.Item));
        } else {
          const u = ti.Update;
          store[u.TableName] ||= {};
          const k = keyOf(u.Key);
          const item = store[u.TableName][k] || { ...u.Key };
          applyUpdate(item, u.UpdateExpression, u.ExpressionAttributeNames, u.ExpressionAttributeValues);
          store[u.TableName][k] = item;
        }
      }
      return {};
    },
  };
}
