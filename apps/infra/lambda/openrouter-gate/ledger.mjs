// OpenRouter holds are separate from Bedrock's shared fast-gate reservation.
// No stale reset: an unresolved generation holds budget until reconciliation.
const N = value => ({ N: String(value) });
const number = item => Number(item?.N ?? 0);

export async function reserve(ddb, env, ownerId, estimate, outbox) {
  if (!Number.isSafeInteger(estimate) || estimate <= 0 || !outbox?.request_id?.S) return false;
  for (let attempt = 0; attempt < 4; attempt++) {
    const row = (await ddb.getItem({ TableName: env.FAST_GATE_TABLE, Key: { owner_id: { S: ownerId } }, ConsistentRead: true }))?.Item;
    if (!row?.remaining_microcents?.N) return false;
    const remaining = number(row.remaining_microcents);
    const bedrock = number(row.reserved_microcents);
    const openrouter = number(row.openrouter_reserved_microcents);
    if (remaining - bedrock - openrouter - estimate <= 0) return false;
    try {
      await ddb.transactWriteItems({ TransactItems: [
        { Update: {
          TableName: env.FAST_GATE_TABLE,
          Key: { owner_id: { S: ownerId } },
          UpdateExpression: 'SET openrouter_reserved_microcents = if_not_exists(openrouter_reserved_microcents, :zero) + :estimate',
          ConditionExpression: 'attribute_exists(owner_id) AND remaining_microcents = :remaining AND (attribute_not_exists(reserved_microcents) OR reserved_microcents = :bedrock) AND (attribute_not_exists(openrouter_reserved_microcents) OR openrouter_reserved_microcents = :openrouter)',
          ExpressionAttributeValues: { ':zero': N(0), ':estimate': N(estimate), ':remaining': N(remaining), ':bedrock': N(bedrock), ':openrouter': N(openrouter) },
        } },
        { Put: { TableName: env.REQUESTS_TABLE, Item: outbox, ConditionExpression: 'attribute_not_exists(request_id)' } },
      ] });
      return true;
    } catch (error) {
      if (error?.name !== 'TransactionCanceledException' || !error.CancellationReasons?.some(reason => reason.Code === 'ConditionalCheckFailed')) throw error;
    }
  }
  return false;
}

export async function release(ddb, env, ownerId, estimate, requestId) {
  await ddb.transactWriteItems({ TransactItems: [
    { Update: {
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: ownerId } },
      UpdateExpression: 'SET openrouter_reserved_microcents = openrouter_reserved_microcents - :estimate',
      ConditionExpression: 'attribute_exists(owner_id) AND openrouter_reserved_microcents >= :estimate',
      ExpressionAttributeValues: { ':estimate': N(estimate) },
    } },
    { Update: {
      TableName: env.REQUESTS_TABLE,
      Key: { request_id: { S: requestId } },
      UpdateExpression: 'SET #state = :released',
      ConditionExpression: '#state = :pending',
      ExpressionAttributeNames: { '#state': 'state' },
      ExpressionAttributeValues: { ':released': { S: 'released' }, ':pending': { S: 'pending' } },
    } },
  ] });
}
