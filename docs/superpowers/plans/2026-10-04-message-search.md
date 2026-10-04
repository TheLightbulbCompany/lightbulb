# Message Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cross-room message search with real ranking (BM25, stemming, phrases, typo tolerance, gentle recency), exposed in ⌘K and the in-room search dialog, without weakening chat's security posture.

**Architecture:** OpenSearch Serverless **NextGen** collection (scale-to-zero), one index per room owner, reachable only through a VPC endpoint, encrypted with the messaging table's KMS key. The messaging projector enqueues `{ownerId, roomId, sk}` on SQS for every message insert/edit (it never talks to OpenSearch, so an indexing cold start can never stall live delivery). A VPC indexer Lambda re-reads the CURRENT DynamoDB row, writes the index, then re-reads again until the row and the index agree (closes the stale-write and wipe races). The backend decides access on every search from DynamoDB membership (`terms` filter + index list); the index never decides access. Search requests are POST bodies so queries never reach access logs.

**Tech Stack:** AWS CDK 2.267 (`aws-opensearchserverless` L1: `CfnCollectionGroup`, `CfnCollection`, `CfnSecurityPolicy`, `CfnAccessPolicy`, `CfnVpcEndpoint`), Python 3.13 Lambdas (boto3/botocore only — SigV4 via `botocore.auth.SigV4Auth`, no new deps), FastAPI + httpx, Next.js/React, vitest, pytest, jest.

**Spec:** This conversation's agreed design, revised after a ponytail review and a correctness/security review. Decisions: OpenSearch over DynamoDB-scan / shared Paperclip Aurora (the Paperclip process connects as the master user; that cluster hits its 4-ACU cap daily) / third-party SaaS (new subprocessor for all chat text). Cost levers: NextGen scale-to-zero (10–30 s cold start), warm-on-open instead of a search floor, standby replicas DISABLED (DynamoDB is the truth; rebuild = re-run the reindex script), max-OCU cap. Security: VPC-only, KMS, two principals, query-time authorization with the same membership rule as the room door, deletes really delete, agents never query the index, no query logging (POST bodies), plain-text snippets.

## Global Constraints

- Three PRs, in order: **PR A** (Tasks 1–5) ships indexing + API to **dev only**; **Task 6** is the owner's cost gate; **PR B** (Task 7) turns prod on and backfills; **PR C** (Tasks 8–10) switches the UI. PR C must not merge before prod serves search (prod frontend ships on every prod dispatch).
- No new Python or npm dependencies. SigV4 is `botocore.auth.SigV4Auth` with service `aoss`; every OpenSearch Serverless request carries `x-amz-content-sha256: <hex sha256 of body>`.
- Index name is `"messages-" + sha256(owner_id).hexdigest()[:32]` (Clerk org ids are mixed-case; index names must be lowercase). Defined twice — `apps/infra/lambda/messaging-search-indexer/index.py:index_name` and `apps/backend/core/services/message_search.py:index_name` — both tests pin `index_name("org_owner") == "messages-603feeb09672e3cefd5c20fc9aa957ba"`.
- Document id is `f"{roomId}:{messageSeq}"` (derivable from the row key, so a job can delete a row that no longer exists).
- Index only PUBLIC text: `editedText` when present, else `text`; nothing for a row with `deletedAt`. An edited message's original request text is never indexed.
- Never log or put in a URL the search query or message text. Search is `POST /messaging/search` with a JSON body. Logs carry ids, status codes and error types only.
- `SEARCH_ENDPOINT` empty ⇒ search is off: the API answers 503 `SEARCH_UNAVAILABLE`; `warm` and `delete_owner_index` are no-ops. Projector: `SEARCH_INDEX_QUEUE_URL` empty ⇒ no enqueue.
- Any change under `apps/infra/lib/` requires `cd apps/infra && npx cdk synth` and committing the regenerated `.github/workflows/deploy.yml` in the same PR.
- Tests: write them; run ONLY the targeted commands each step names (owner rule: CI is the full gate). Backend: `cd apps/backend && uv run pytest <file> -q`. Lambdas: `cd apps/backend && uv run pytest ../infra/lambda/<dir> -q`. Infra: `cd apps/infra && npx jest test/api-stack.test.ts`. Frontend: `cd apps/frontend && pnpm exec vitest run <file>`, then `pnpm exec vitest related --run <changed files>` and `pnpm exec eslint <changed files>` (`react-hooks/set-state-in-effect` is an error here: never call setState synchronously in an effect body).
- Fresh worktree deps: `uv sync` in apps/backend; symlink `node_modules`, `apps/frontend/node_modules`, `packages/shared/node_modules`, `apps/infra/node_modules` from `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync`; remove symlinks before committing.

## Review Focus

1. **Removed member, or a foreign member viewing from the wrong org** → no results from that room, immediately. Task 4 tests: `searchable_rooms` applies `member_home(member, meta.ownerId) == owner_id` like `require_room_member`; `roomId=` for an unsearchable room is `ROOM_NOT_FOUND` with no index call.
2. **Edited or deleted message, even when jobs race** → never searchable by old text. Task 3 tests: after a PUT, a re-read that shows a newer revision or a delete makes the indexer re-PUT or DELETE.
3. **Wiped workspace while index jobs are in flight** → no wiped text survives. Task 3 test: PUT then re-read finds the row gone → DELETE. Task 4: wipes drop the owner's index as their own step.
4. **First search after 10 idle minutes** (cold start up to 30 s; API Gateway caps at 30 s) → "Search is starting up…" and one automatic retry, not a generic error. Task 4: 25 s timeout or 5xx → 503 `SEARCH_UNAVAILABLE`; a 4xx from OpenSearch → 500 (a policy mistake must not look like warm-up forever). Task 10 test: 503 shows the warming copy and retries once.
5. **Operators** (`"exact phrase"`, `-exclude`) behave as typed: Task 4 test — the fuzzy clause is added only to operator-free queries.

---

## PR A — indexing + API (dev only)

### Task 1: Infrastructure

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts` (new block after the projector's grants, ~line 700; DLQ alarm after the existing `pageAlarm("OpenClawWorkPipeDlqVisibleAlarm", …)` ~line 907; new public fields near the other `public readonly` messaging names)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props interface ~line 173; env map ~line 1462; `messagingPolicy` ~line 347)
- Modify: `apps/infra/lib/lightbulb-stage.ts` (ServiceStack props ~line 249)
- Modify: `.github/workflows/backend.yml` (path filter + test step)
- Modify: `.github/workflows/deploy.yml` (regenerated by `npx cdk synth` only)
- Create: `apps/infra/lambda/messaging-search-indexer/index.py` — placeholder so the asset exists (Task 3 replaces it):
  ```python
  def handler(event, context):
      raise NotImplementedError("message search indexer lands in Task 3")
  ```
- Test: `apps/infra/test/api-stack.test.ts` (new `describe` block + three existing tests updated)

**Interfaces:**
- Produces (ApiStack public fields; empty strings when search is off for the env): `messageSearchEndpoint: string`, `messageSearchCollectionArn: string`, `messageSearchQueueUrl: string`.
- Produces (ServiceStack optional props): `messageSearchEndpoint?: string`, `messageSearchCollectionArn?: string` → backend env `SEARCH_ENDPOINT`.
- Produces (Lambda env): projector `SEARCH_INDEX_QUEUE_URL`; indexer `MESSAGING_TABLE`, `SEARCH_ENDPOINT`.

- [ ] **Step 1: Write the failing infra tests.** Append:

```ts
describe("ApiStack — message search (OpenSearch Serverless)", () => {
  const dev = API_TEMPLATE;

  test("a NextGen collection group with no standby replicas and capped OCUs", () => {
    dev.hasResourceProperties("AWS::OpenSearchServerless::CollectionGroup", {
      Generation: "NEXTGEN",
      StandbyReplicas: "DISABLED",
      CapacityLimits: {
        MinIndexingCapacityInOcu: 0,
        MinSearchCapacityInOcu: 0,
        MaxIndexingCapacityInOcu: 2,
        MaxSearchCapacityInOcu: 2,
      },
    });
    dev.hasResourceProperties("AWS::OpenSearchServerless::Collection", {
      Name: "lightbulb-dev-messages",
      Type: "SEARCH",
      EncryptionConfig: Match.objectLike({ AWSOwnedKey: false }),
    });
  });

  test("the collection is reachable only through the VPC endpoint", () => {
    const policies = Object.values(dev.findResources("AWS::OpenSearchServerless::SecurityPolicy")) as any[];
    const network = policies.find((p) => p.Properties.Type === "network");
    expect(JSON.stringify(network.Properties.Policy)).toContain('\\"AllowFromPublic\\":false');
    dev.resourceCountIs("AWS::OpenSearchServerless::VpcEndpoint", 1);
  });

  test("exactly two principals: the indexer writes, the backend reads and drops indexes", () => {
    const access = Object.values(dev.findResources("AWS::OpenSearchServerless::AccessPolicy")) as any[];
    expect(access).toHaveLength(1);
    const text = JSON.stringify(access[0].Properties.Policy);
    expect(text).toContain("role/lightbulb-dev-backend-task");
    expect(text).toContain("aoss:WriteDocument");
    expect(text).toContain("aoss:DeleteIndex");
    expect(text).not.toContain("aoss:*");
  });

  test("the projector only enqueues; the indexer runs in the VPC, one job at a time", () => {
    dev.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("messaging-projector"),
      Environment: { Variables: Match.objectLike({ SEARCH_INDEX_QUEUE_URL: Match.anyValue() }) },
    });
    dev.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("message-search-indexer"),
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
    });
    dev.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 1,
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      ScalingConfig: { MaximumConcurrency: 5 },
    });
  });

  test("a stuck index job pages", () => {
    dev.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-message-search-index-dlq-visible",
    });
  });

  test("prod has no search resources until the cost gate (plan Task 7)", () => {
    const prod = synthApiStack("prod");
    prod.resourceCountIs("AWS::OpenSearchServerless::Collection", 0);
  });
});
```

Update the three existing tests the new resources break:
- `"creates one application FIFO plus its FIFO DLQ and one standard Pipe DLQ"` (~line 293): add `"lightbulb-dev-message-search-index"` and `"lightbulb-dev-message-search-index-dlq"` to the sorted expected list (keep it sorted) and adjust the test title.
- `"messaging stream has exactly the projector and direct Pipe consumers"` (~line 481): the stream still has exactly those consumers; change `resourceCountIs("AWS::Lambda::EventSourceMapping", 1)` into a count of mappings whose `EventSourceArn` is the messaging table's stream (filter `findResources("AWS::Lambda::EventSourceMapping")` by `Properties.EventSourceArn` containing `StreamArn`) equal to 1.
- The projector-grants test (~line 722): replace `expect([...granted].filter((a) => a.startsWith("sqs:"))).toEqual([])` with an assertion that every `sqs:` action is a send action (`sqs:SendMessage`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl`) and that their resources reference only the search index queue.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/infra && npx jest test/api-stack.test.ts`
Expected: FAIL — the new block (no `AWS::OpenSearchServerless::*`) and the updated queue-list test.

- [ ] **Step 3: Implement**

`api-stack.ts` — `import * as aoss from "aws-cdk-lib/aws-opensearchserverless";`. Public fields:

```ts
  /** Message search (OpenSearch Serverless). Empty strings while search is off for this env. */
  public readonly messageSearchEndpoint: string;
  public readonly messageSearchCollectionArn: string;
  public readonly messageSearchQueueUrl: string;
```

Block after the projector's grants (declare `let indexDlq: sqs.Queue | undefined;` just before it so the alarm below can reference it):

```ts
    // ---- Message search: OpenSearch Serverless NextGen ----
    // The projector only ENQUEUES a row pointer; a VPC indexer re-reads the row
    // and writes the index, so an indexing cold start (10–30 s) can never stall
    // live delivery on this stream shard. DynamoDB stays the truth: standby
    // replicas are off and scripts/reindex_messages.py rebuilds an index.
    // The collection's endpoint and ARN cross to ServiceStack as CloudFormation
    // exports, and Name/Tags/EncryptionConfig/CollectionGroupName force
    // REPLACEMENT: a new stage-wide tag (lightbulb-stage.ts applies cdk.Tags)
    // replaces the collection, blocks on the export, and empties the index.
    // Plan such a change as: drop the import, deploy, change, re-import, reindex.
    // ponytail: dev only until the measured-cost gate (plan Task 6/7).
    const searchEnabled = env === "dev";
    if (searchEnabled) {
      const collectionName = `lightbulb-${env}-messages`;
      const group = new aoss.CfnCollectionGroup(this, "MessageSearchGroup", {
        name: `lightbulb-${env}-search`,
        generation: "NEXTGEN",
        standbyReplicas: "DISABLED",
        capacityLimits: {
          minIndexingCapacityInOcu: 0,
          minSearchCapacityInOcu: 0,
          maxIndexingCapacityInOcu: 2,
          maxSearchCapacityInOcu: 2,
        },
      });
      const endpointSg = new ec2.SecurityGroup(this, "MessageSearchEndpointSg", {
        vpc: props.vpc,
        description: "OpenSearch Serverless VPC endpoint (message search)",
        allowAllOutbound: false,
      });
      endpointSg.addIngressRule(ec2.Peer.ipv4(props.vpc.vpcCidrBlock), ec2.Port.tcp(443), "VPC to message search");
      const vpce = new aoss.CfnVpcEndpoint(this, "MessageSearchVpce", {
        name: `lightbulb-${env}-msg-vpce`,
        vpcId: props.vpc.vpcId,
        subnetIds: props.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds,
        securityGroupIds: [endpointSg.securityGroupId],
      });
      const network = new aoss.CfnSecurityPolicy(this, "MessageSearchNetwork", {
        name: `lightbulb-${env}-msg-net`,
        type: "network",
        policy: cdk.Stack.of(this).toJsonString([{
          Rules: [{ ResourceType: "collection", Resource: [`collection/${collectionName}`] }],
          AllowFromPublic: false,
          SourceVPCEs: [vpce.attrId],
        }]),
      });
      const collection = new aoss.CfnCollection(this, "MessageSearchCollection", {
        name: collectionName,
        type: "SEARCH",
        collectionGroupName: group.name, // standby replicas follow the group
        // Same key as the messaging table: search holds the same data.
        encryptionConfig: { awsOwnedKey: false, kmsKeyArn: props.paperclipKmsKeyArn },
      });
      collection.addDependency(group);
      collection.addDependency(network);

      indexDlq = new sqs.Queue(this, "MessageSearchIndexDlq", {
        queueName: `${resourcePrefix(env)}message-search-index-dlq`,
        retentionPeriod: cdk.Duration.days(14),
        encryption: sqs.QueueEncryption.SQS_MANAGED,
      });
      const indexQueue = new sqs.Queue(this, "MessageSearchIndexQueue", {
        queueName: `${resourcePrefix(env)}message-search-index`,
        // AWS guidance: visibility ≥ 6× the consumer's timeout (120 s).
        visibilityTimeout: cdk.Duration.minutes(15),
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        deadLetterQueue: { queue: indexDlq, maxReceiveCount: 5 },
      });

      const indexer = new lambda.Function(this, "MessageSearchIndexer", {
        functionName: `${resourcePrefix(env)}message-search-indexer`,
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "index.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "..", "..", "lambda", "messaging-search-indexer")),
        // One job per invocation: a cold start (≤30 s) plus a write/verify loop fits.
        timeout: cdk.Duration.seconds(120),
        memorySize: 256,
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        environment: {
          MESSAGING_TABLE: messagingTable.tableName,
          SEARCH_ENDPOINT: collection.attrCollectionEndpoint,
        },
        logGroup: new logs.LogGroup(this, "MessageSearchIndexerLogs", {
          logGroupName: logGroupPath(env, "message-search-indexer"),
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      });
      messagingTable.grantReadData(indexer);
      indexer.addToRolePolicy(new iam.PolicyStatement({ actions: ["aoss:APIAccessAll"], resources: [collection.attrArn] }));
      indexer.addEventSource(new lambdaEventSources.SqsEventSource(indexQueue, {
        batchSize: 1,
        reportBatchItemFailures: true,
        maxConcurrency: 5, // a backfill must not fan out past the OCU cap
      }));
      indexQueue.grantSendMessages(projectorFn);
      projectorFn.addEnvironment("SEARCH_INDEX_QUEUE_URL", indexQueue.queueUrl);

      // MUST match service-stack's `roleName: lightbulb-${env}-backend-task`. The
      // provisioner Lambda runs as this role too, so wipes reach DeleteIndex.
      const backendRoleArn = `arn:aws:iam::${this.account}:role/lightbulb-${env}-backend-task`;
      const indexes = [`index/${collectionName}/messages-*`];
      new aoss.CfnAccessPolicy(this, "MessageSearchAccess", {
        name: `lightbulb-${env}-msg-access`,
        type: "data",
        policy: cdk.Stack.of(this).toJsonString([
          {
            Rules: [{ ResourceType: "index", Resource: indexes, Permission: ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:WriteDocument"] }],
            Principal: [indexer.role!.roleArn],
          },
          {
            Rules: [{ ResourceType: "index", Resource: indexes, Permission: ["aoss:ReadDocument", "aoss:DescribeIndex", "aoss:DeleteIndex"] }],
            Principal: [backendRoleArn],
          },
        ]),
      });

      this.messageSearchEndpoint = collection.attrCollectionEndpoint;
      this.messageSearchCollectionArn = collection.attrArn;
      this.messageSearchQueueUrl = indexQueue.queueUrl;
    } else {
      this.messageSearchEndpoint = "";
      this.messageSearchCollectionArn = "";
      this.messageSearchQueueUrl = "";
    }
```

After the existing `pageAlarm("OpenClawWorkPipeDlqVisibleAlarm", …)` call (where `pageAlarm` is in scope):

```ts
    if (indexDlq) {
      pageAlarm("MessageSearchIndexDlqVisibleAlarm", {
        alarmName: `${resourcePrefix(env)}message-search-index-dlq-visible`,
        alarmDescription: "PAGE: message search index jobs exhausted retries; search may show stale or deleted text until replayed.",
        metric: indexDlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(1), statistic: "Maximum" }),
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }
```

`service-stack.ts` props (optional, so other constructions keep compiling):

```ts
  /** Message search collection; empty/undefined while search is off for this env. */
  messageSearchEndpoint?: string;
  messageSearchCollectionArn?: string;
```

Env map next to `MESSAGING_TABLE`: `SEARCH_ENDPOINT: props.messageSearchEndpoint ?? "",`. In `messagingPolicy`, when `props.messageSearchCollectionArn` is truthy, add `new iam.PolicyStatement({ actions: ["aoss:APIAccessAll"], resources: [props.messageSearchCollectionArn] })` using whatever form that policy already uses for its statements.

`lightbulb-stage.ts` ServiceStack props: `messageSearchEndpoint: api.messageSearchEndpoint, messageSearchCollectionArn: api.messageSearchCollectionArn,`.

`.github/workflows/backend.yml`: add `- 'apps/infra/lambda/messaging-search-indexer/**'` to `paths:`, and after the projector step:

```yaml
      - name: Run messaging-search-indexer Lambda tests
        env:
          AWS_DEFAULT_REGION: us-east-1
          AWS_ACCESS_KEY_ID: testing
          AWS_SECRET_ACCESS_KEY: testing
        run: uv run pytest ../infra/lambda/messaging-search-indexer -v
        working-directory: apps/backend
```

- [ ] **Step 4: Run tests, then synth**

Run: `cd apps/infra && npx jest test/api-stack.test.ts` → PASS (whole file — it is one stack's tests).
Run: `cd apps/infra && ls test | grep -i service` and run that file if it exists → PASS.
Run: `cd apps/infra && npx cdk synth > /dev/null` → exit 0; commit the regenerated `.github/workflows/deploy.yml`.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib apps/infra/test/api-stack.test.ts apps/infra/lambda/messaging-search-indexer/index.py .github/workflows/backend.yml .github/workflows/deploy.yml
git commit -m "feat(infra): OpenSearch Serverless message search (dev)"
```

---

### Task 2: Projector enqueues index jobs

**Files:**
- Modify: `apps/infra/lambda/messaging-projector/index.py`
- Test: `apps/infra/lambda/messaging-projector/test_index.py` (new `SearchEnqueueTests`)

**Interfaces:**
- Consumes: env `SEARCH_INDEX_QUEUE_URL` (Task 1).
- Produces: SQS body `{"ownerId": str, "roomId": str, "sk": "MSG#<20 digits>"}` (compact JSON). Task 3 consumes it.

- [ ] **Step 1: Write the failing tests** (append; reuse `FakeDdb`, `message_item`, `stream_record`, `ROOM_ID`, `DELIVERY_KEY`, `WORKSPACE_ID`):

```python
class SearchEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index.HTTP_ENDPOINT, index.REGION, index._ddb_client,
                      index.SEARCH_INDEX_QUEUE_URL, index._sqs_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.HTTP_ENDPOINT = "https://example.appsync-api.us-east-1.amazonaws.com/event"
        index.REGION = "us-east-1"
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": "org_room_owner", "workspaceId": WORKSPACE_ID}})
        index.SEARCH_INDEX_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/1/search"
        index._sqs_client = MagicMock()

    def tearDown(self):
        (index.MESSAGING_TABLE, index.HTTP_ENDPOINT, index.REGION, index._ddb_client,
         index.SEARCH_INDEX_QUEUE_URL, index._sqs_client) = self.saved

    def _body(self):
        return json.loads(index._sqs_client.send_message.call_args.kwargs["MessageBody"])

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_new_message_enqueues_with_the_room_owner_before_push(self, publish, notify, push):
        order = []
        index._sqs_client.send_message.side_effect = lambda **_: order.append("enqueue")
        push.side_effect = lambda *a, **k: order.append("push")
        index.process_record(stream_record(message_item()))
        self.assertEqual(self._body(), {"ownerId": "org_room_owner", "roomId": ROOM_ID, "sk": message_item()["sk"]})
        self.assertEqual(order, ["enqueue", "push"])

    @patch("index._publish")
    def test_edit_enqueues(self, publish):
        item = {**message_item(), "messageRevision": 2, "editedText": "new words", "editedAt": "2026-10-04T00:00:01Z"}
        record = stream_record(item)
        record["eventName"] = "MODIFY"
        index.process_record(record)
        self.assertEqual(self._body()["sk"], item["sk"])

    @patch("index._publish")
    def test_edit_enqueues_even_when_the_workspace_is_unresolvable(self, publish):
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": "org_room_owner"}})
        item = {**message_item(), "messageRevision": 2, "editedText": None, "deletedAt": "2026-10-04T00:00:02Z",
                "editedAt": "2026-10-04T00:00:02Z"}
        with patch("index._workspace_id_for_meta", return_value=None):
            record = stream_record(item)
            record["eventName"] = "MODIFY"
            index.process_record(record)
        self.assertEqual(self._body()["sk"], item["sk"])
        publish.assert_not_called()

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_search_off_enqueues_nothing(self, publish, notify, push):
        index.SEARCH_INDEX_QUEUE_URL = ""
        index.process_record(stream_record(message_item()))
        index._sqs_client.send_message.assert_not_called()

    @patch("index._publish")
    def test_reaction_modify_enqueues_nothing(self, publish):
        record = stream_record({**message_item(), "reactionRevision": 1})
        record["eventName"] = "MODIFY"
        index.process_record(record)
        index._sqs_client.send_message.assert_not_called()
```

(Read `sk` from `message_item()`; never hard-code it.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest ../infra/lambda/messaging-projector -q -k SearchEnqueue`
Expected: FAIL — `AttributeError: module 'index' has no attribute 'SEARCH_INDEX_QUEUE_URL'`.

- [ ] **Step 3: Implement** — module level, after `USERS_TABLE`:

```python
SEARCH_INDEX_QUEUE_URL = os.environ.get("SEARCH_INDEX_QUEUE_URL", "")
_sqs_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _enqueue_search_index(item: dict[str, Any], meta: dict[str, Any]) -> None:
    """Point the search indexer at this row. The indexer re-reads the row, so
    duplicates and reordering are harmless. Keyed on the ROOM owner (META), whose
    index holds the room -- a shared room's foreign members search it there."""
    if not SEARCH_INDEX_QUEUE_URL:
        return
    owner_id = meta.get("ownerId")
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("room metadata has no owner")
    _sqs().send_message(
        QueueUrl=SEARCH_INDEX_QUEUE_URL,
        MessageBody=json.dumps({"ownerId": owner_id, "roomId": item["roomId"], "sk": item["sk"]}, separators=(",", ":")),
    )
```

In `_project_message_update` and in `_project_message`, call `_enqueue_search_index(item, meta)` immediately after the `if not meta: raise ValueError("room metadata is missing")` check — before the `workspace_id is None` early return, so an edit or delete in an unresolvable workspace still reaches the index; and (in `_project_message`) before the push at the end, so a failed enqueue replays the record before anyone is notified.

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest ../infra/lambda/messaging-projector -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lambda/messaging-projector/index.py apps/infra/lambda/messaging-projector/test_index.py
git commit -m "feat(messaging): projector enqueues message search index jobs"
```

---

### Task 3: Search indexer Lambda

**Files:**
- Modify (replace placeholder): `apps/infra/lambda/messaging-search-indexer/index.py`
- Create: `apps/infra/lambda/messaging-search-indexer/test_index.py`

**Interfaces:**
- Consumes: SQS body from Task 2; env `MESSAGING_TABLE`, `SEARCH_ENDPOINT`, `AWS_REGION`.
- Produces: documents in `index_name(ownerId)` with id `f"{roomId}:{seq}"` and fields `roomId, messageId, messageSeq, createdAt, text, authorName, authorKind, authorId, threadRootMessageId` (Task 4 queries these names).

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import index

ROOM = "11111111-1111-4111-8111-111111111111"
SK = "MSG#00000000000000000007"
TARGET = f"/{index.index_name('org_owner')}/_doc/{ROOM}:7"


def row(**extra):
    return {
        "pk": f"ROOM#{ROOM}", "sk": SK, "roomId": ROOM, "messageId": "m-1", "messageSeq": 7,
        "createdAt": "2026-10-04T00:00:00Z", "text": "ship the deploy fix",
        "author": {"kind": "human", "id": "user_a", "displayName": "Ann"}, **extra,
    }


def job():
    return {"ownerId": "org_owner", "roomId": ROOM, "sk": SK}


def calls(request):
    return [(c.args[0], c.args[1]) for c in request.call_args_list]


class IndexerTests(unittest.TestCase):
    def test_index_name_matches_the_backend_vector(self):
        self.assertEqual(index.index_name("org_owner"), "messages-603feeb09672e3cefd5c20fc9aa957ba")

    def test_document_indexes_public_text_only(self):
        self.assertEqual(index.document(row())["text"], "ship the deploy fix")
        edited = index.document(row(editedText="new words"))
        self.assertEqual(edited["text"], "new words")
        self.assertNotIn("ship", json.dumps(edited))
        self.assertIsNone(index.document(row(deletedAt="2026-10-04T00:01:00Z")))

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row()])
    def test_live_row_is_written_then_verified(self, get_row, request):
        index.process(job())
        name = index.index_name("org_owner")
        self.assertEqual(calls(request), [("PUT", f"/{name}"), ("PUT", TARGET)])
        self.assertEqual(get_row.call_count, 2)

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row(deletedAt="2026-10-04T00:01:00Z")])
    def test_a_delete_that_lands_after_our_write_is_applied(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request)[-1], ("DELETE", TARGET))

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row(editedText="newer", messageRevision=2), row(editedText="newer", messageRevision=2)])
    def test_an_edit_that_lands_after_our_write_is_rewritten(self, get_row, request):
        index.process(job())
        last = request.call_args_list[-1]
        self.assertEqual((last.args[0], last.args[1]), ("PUT", TARGET))
        self.assertEqual(last.args[2]["text"], "newer")

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), None])
    def test_a_wipe_that_lands_after_our_write_removes_it(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request)[-1], ("DELETE", TARGET))

    @patch("index._request", return_value=(404, b"{}"))
    @patch("index._get_row", return_value=None)
    def test_missing_row_deletes_and_accepts_404(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request), [("DELETE", TARGET)])

    @patch("index._request", side_effect=[(400, b'{"error":{"type":"resource_already_exists_exception"}}'), (200, b"{}")])
    @patch("index._get_row", side_effect=[row(), row()])
    def test_existing_index_is_fine(self, get_row, request):
        index.process(job())

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(editedText=f"v{n}", messageRevision=n) for n in range(1, 20)])
    def test_a_row_that_never_settles_fails_the_job_for_retry(self, get_row, request):
        with self.assertRaises(RuntimeError):
            index.process(job())

    @patch("index.process", side_effect=RuntimeError("boom"))
    def test_handler_reports_the_failed_job(self, process):
        event = {"Records": [{"messageId": "b", "body": json.dumps(job())}]}
        self.assertEqual(index.handler(event, None), {"batchItemFailures": [{"itemIdentifier": "b"}]})

    def test_rejects_non_message_rows(self):
        with self.assertRaises(ValueError):
            index.process({"ownerId": "org_owner", "roomId": ROOM, "sk": "META"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest ../infra/lambda/messaging-search-indexer -q`
Expected: FAIL — `AttributeError: module 'index' has no attribute 'index_name'`.

- [ ] **Step 3: Implement** `index.py`:

```python
"""Index one message per SQS job into OpenSearch Serverless (message search).

A job names a row ({ownerId, roomId, sk}). The indexer re-reads the CURRENT row,
writes it (or deletes it when the row is deleted or gone), then re-reads again and
repeats until the row it last wrote is still the row in DynamoDB. That loop is what
keeps a slow job from putting back a deleted message, an edited message's old text,
or a wiped workspace's text. Only public text is indexed."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

import boto3
import botocore.session
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MESSAGING_TABLE = os.environ.get("MESSAGING_TABLE", "")
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_ROUNDS = 5

MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "roomId": {"type": "keyword"},
            "messageId": {"type": "keyword"},
            "messageSeq": {"type": "long"},
            "createdAt": {"type": "date"},
            "text": {"type": "text", "analyzer": "english"},
            "authorName": {"type": "text"},
            "authorKind": {"type": "keyword"},
            "authorId": {"type": "keyword"},
            "threadRootMessageId": {"type": "keyword"},
        },
    }
}

_ddb_client = None
_deserializer = TypeDeserializer()


def index_name(owner_id: str) -> str:
    # MUST match apps/backend/core/services/message_search.py:index_name.
    return "messages-" + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def doc_id(room_id: str, sk: str) -> str:
    return f"{room_id}:{int(sk.removeprefix('MSG#'))}"


def document(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("deletedAt"):
        return None
    author = item.get("author") or {}
    return {
        "roomId": item["roomId"],
        "messageId": item["messageId"],
        "messageSeq": int(item["messageSeq"]),
        "createdAt": item["createdAt"],
        "text": item["editedText"] if "editedText" in item else item.get("text") or "",
        "authorName": author.get("displayName") or "",
        "authorKind": author.get("kind") or "",
        "authorId": author.get("id") or "",
        "threadRootMessageId": item.get("threadRootMessageId"),
    }


def _ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _get_row(room_id: str, sk: str) -> dict[str, Any] | None:
    response = _ddb().get_item(
        TableName=MESSAGING_TABLE,
        Key={"pk": {"S": f"ROOM#{room_id}"}, "sk": {"S": sk}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return {key: _deserializer.deserialize(value) for key, value in item.items()} if item else None


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else b""
    request = AWSRequest(
        method=method,
        url=f"{SEARCH_ENDPOINT}{path}",
        data=data,
        headers={"content-type": "application/json", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()},
    )
    credentials = botocore.session.get_session().get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "aoss", REGION).add_auth(request)
    prepared = request.prepare()
    outbound = urllib.request.Request(
        prepared.url, data=prepared.body or None, headers=dict(prepared.headers.items()), method=method
    )
    try:
        # 40 s: a NextGen cold start takes 10–30 s.
        with urllib.request.urlopen(outbound, timeout=40) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _ensure_index(name: str) -> None:
    # ponytail: one PUT per write, no in-process cache -- a cache would go stale
    # when a wipe drops the index. If a wipe drops the index between this PUT and
    # the doc PUT, aoss auto-creates a dynamic-mapped index; the verify loop then
    # deletes the doc, leaving an empty mis-mapped index for that owner. Fix by
    # deleting it (scripts/reindex_messages.py rebuilds) if it is ever seen.
    status, raw = _request("PUT", f"/{name}", MAPPING)
    if status >= 300 and b"resource_already_exists" not in raw:
        raise RuntimeError(f"create index failed status={status}")


def process(job: dict[str, Any]) -> None:
    owner_id, room_id, sk = job["ownerId"], job["roomId"], job["sk"]
    if not isinstance(sk, str) or not sk.startswith("MSG#"):
        raise ValueError("job does not name a message row")
    name = index_name(owner_id)
    target = f"/{name}/_doc/{doc_id(room_id, sk)}"
    item = _get_row(room_id, sk)
    for _ in range(MAX_ROUNDS):
        doc = document(item) if item else None
        if doc is None:
            status, _ = _request("DELETE", target)
            if status not in (200, 404):
                raise RuntimeError(f"delete failed status={status}")
            return  # a racing writer's own verify read will see this delete too
        _ensure_index(name)
        status, _ = _request("PUT", target, doc)
        if status not in (200, 201):
            raise RuntimeError(f"index failed status={status}")
        item = _get_row(room_id, sk)
        if item and document(item) == doc:
            return
    raise RuntimeError("row kept changing; retrying the job")


def handler(event: dict, context: object) -> dict:
    del context
    failures = []
    for record in event.get("Records", []):
        try:
            process(json.loads(record["body"]))
        except Exception as exc:  # noqa: BLE001 - partial-batch contract
            logger.error("search index job failed message_id=%s error_type=%s", record.get("messageId"), type(exc).__name__)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest ../infra/lambda/messaging-search-indexer -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lambda/messaging-search-indexer
git commit -m "feat(messaging): message search indexer Lambda"
```

---

### Task 4: Backend search API, warm-up, and wipe cleanup

**Files:**
- Create: `apps/backend/core/services/message_search.py`
- Modify: `apps/backend/core/services/messaging.py` (`"SEARCH_UNAVAILABLE": 503` in `API_ERROR_STATUS` ~line 134; `_visible_room_metas` + `searchable_rooms` next to `_room_summaries` ~line 1657, and `_room_summaries` reuses `_visible_room_metas`)
- Modify: `apps/backend/core/config.py` (`SEARCH_ENDPOINT: str = ""` next to `MESSAGING_TABLE` ~line 441)
- Modify: `apps/backend/routers/messaging.py` (two routes next to `GET /messaging/rooms/{room_id}/messages` ~line 660)
- Modify: `apps/backend/core/services/account_wipe.py` (a new `_step` right after the messaging step)
- Modify: `apps/backend/core/services/workspace_wipe.py` (a new `WorkspaceStepDef` right after the `"messaging"` one ~line 904, same `LAYER_MESSAGING`)
- Test: `apps/backend/tests/unit/services/test_message_search.py`
- Test: one case each in `tests/unit/services/test_account_wipe.py` and `tests/unit/services/test_workspace_wipe.py`

**Interfaces:**
- Consumes: Task 3 field names; in `core/services/messaging.py`: `messaging_repo.list_rooms_for_member`, `messaging_repo.get_member`, `messaging_repo.member_home(member, owner_id)`, `_SUMMARY_CONCURRENCY`, `ContractModel`, `ApiProblem` (code lives at `.error.code`), `API_ERROR_STATUS` (has `INTERNAL_ERROR: 500`); `settings.AWS_REGION`; `resolve_owner_id` from `core.auth`.
- Produces:
  - `messaging.searchable_rooms(owner_id: str, user_id: str) -> dict[str, str]` (room id → room owner id)
  - `message_search.MessageSearchRequest` (`q: str` 1–200 chars, `room_id: uuid.UUID | None`, `limit: int` 1–50, default 30; camelCase on the wire via `ContractModel`)
  - `message_search.index_name`, `build_query(q, room_ids, limit) -> dict`, `search(auth, request) -> MessageSearchResponse`, `warm(auth) -> None`, `delete_owner_index(owner_id) -> str`
  - HTTP: `POST /api/v1/messaging/search` body `{"q", "roomId"?, "limit"?}` → `{"hits": [{"messageId","roomId","messageSeq","threadRootMessageId","authorName","authorKind","createdAt","snippet"}]}`; `POST /api/v1/messaging/search/warm` → 204. Snippets mark matches with `\u0001` … `\u0002` (rendered as `<mark>` by Task 9; never HTML).

- [ ] **Step 1: Write the failing tests** — `tests/unit/services/test_message_search.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import message_search
from core.services.messaging import ApiProblem

ROOM_A = "11111111-1111-4111-8111-111111111111"
ROOM_B = "22222222-2222-4222-8222-222222222222"


def _auth():
    auth = MagicMock()
    auth.org_id = "org_me"
    auth.user_id = "user_a"
    return auth


def _req(q="deploy", room_id=None, limit=30):
    return message_search.MessageSearchRequest(q=q, room_id=room_id, limit=limit)


def _response(status, body):
    return httpx.Response(status, content=json.dumps(body).encode())


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "https://c.us-east-1.aoss.amazonaws.com")
    monkeypatch.setattr(message_search, "resolve_owner_id", lambda auth: "org_me")


def _bool(query):
    return query["query"]["function_score"]["query"]["bool"]


def test_index_name_matches_the_indexer_vector():
    assert message_search.index_name("org_owner") == "messages-603feeb09672e3cefd5c20fc9aa957ba"


def test_query_always_filters_to_the_given_rooms_and_keeps_old_messages_findable():
    query = message_search.build_query("deploy fix", [ROOM_A], 30)
    assert _bool(query)["filter"] == [{"terms": {"roomId": [ROOM_A]}}]
    scoring = query["query"]["function_score"]
    assert scoring["score_mode"] == "sum"
    assert {"weight": 0.5} in scoring["functions"]  # floor: an old exact match still ranks


def test_operators_turn_off_the_fuzzy_clause():
    assert len(_bool(message_search.build_query("deploy fix", [ROOM_A], 30))["should"]) == 2
    for q in ['"deploy fix"', "deploy -prod", "depl*"]:
        assert len(_bool(message_search.build_query(q, [ROOM_A], 30))["should"]) == 1


async def test_search_queries_only_the_callers_rooms_in_their_owners_indexes():
    rooms = {ROOM_A: "org_me", ROOM_B: "org_vendor"}
    hit = {"_source": {"messageId": "m1", "roomId": ROOM_B, "messageSeq": 4, "threadRootMessageId": None,
                       "authorName": "Ann", "authorKind": "human", "createdAt": "2026-10-04T00:00:00Z", "text": "x"},
           "highlight": {"text": ["the \u0001deploy\u0002 fix"]}}
    request = AsyncMock(return_value=_response(200, {"hits": {"hits": [hit]}}))
    with patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value=rooms)), \
         patch.object(message_search, "_request", request):
        result = await message_search.search(_auth(), _req())
    method, path, body = request.await_args.args[:3]
    assert method == "POST"
    for owner in ("org_me", "org_vendor"):
        assert message_search.index_name(owner) in path
    assert sorted(_bool(body)["filter"][0]["terms"]["roomId"]) == sorted(rooms)
    assert result.hits[0].snippet == "the \u0001deploy\u0002 fix"
    assert str(result.hits[0].room_id) == ROOM_B


async def test_a_room_the_caller_cannot_read_is_not_searchable():
    with patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})), \
         patch.object(message_search, "_request", AsyncMock()) as request:
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req(room_id=ROOM_B))
    assert error.value.error.code == "ROOM_NOT_FOUND"
    request.assert_not_awaited()


async def test_no_rooms_means_no_request():
    with patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={})), \
         patch.object(message_search, "_request", AsyncMock()) as request:
        assert (await message_search.search(_auth(), _req())).hits == []
    request.assert_not_awaited()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), _response(503, {})])
async def test_cold_start_is_a_retryable_503(failure):
    request = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    with patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})), \
         patch.object(message_search, "_request", request):
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req())
    assert error.value.error.code == "SEARCH_UNAVAILABLE"
    assert error.value.status_code == 503


async def test_a_policy_mistake_is_not_reported_as_warm_up():
    with patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})), \
         patch.object(message_search, "_request", AsyncMock(return_value=_response(403, {}))):
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req())
    assert error.value.error.code == "INTERNAL_ERROR"


async def test_search_off_is_a_503(monkeypatch):
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "")
    with pytest.raises(ApiProblem) as error:
        await message_search.search(_auth(), _req())
    assert error.value.error.code == "SEARCH_UNAVAILABLE"


async def test_delete_owner_index_accepts_404_and_is_a_noop_when_off(monkeypatch):
    with patch.object(message_search, "_request", AsyncMock(return_value=_response(404, {}))) as request:
        await message_search.delete_owner_index("org_me")
    assert request.await_args.args[:2] == ("DELETE", f"/{message_search.index_name('org_me')}")
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "")
    with patch.object(message_search, "_request", AsyncMock()) as request:
        await message_search.delete_owner_index("org_me")
    request.assert_not_awaited()


async def test_searchable_rooms_uses_the_room_doors_membership_rule():
    from core.services import messaging
    discovered = [
        {"meta": {"roomId": ROOM_A, "ownerId": "org_me"}},
        {"meta": {"roomId": ROOM_B, "ownerId": "org_vendor"}},
        {"meta": {"roomId": "gone", "ownerId": "org_me", "deletedAt": "x"}},
        {"meta": {"roomId": "meet", "ownerId": "org_me", "hidden": True}},
    ]
    members = {ROOM_A: None, ROOM_B: {"id": "user_a", "homeOwnerId": "org_me"}}
    with patch.object(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=discovered)), \
         patch.object(messaging.messaging_repo, "get_member", AsyncMock(side_effect=lambda room, kind, user: members.get(room))), \
         patch.object(messaging.messaging_repo, "member_home", side_effect=lambda member, owner: member.get("homeOwnerId", owner)):
        assert await messaging.searchable_rooms("org_me", "user_a") == {ROOM_B: "org_vendor"}
```

In `test_account_wipe.py` and `test_workspace_wipe.py`, find the test that drives the messaging step (grep `delete_owner_partition`) and add a case asserting the new search step calls `message_search.delete_owner_index` with the owner id and is recorded as its own step (`system="search"`, `action="drop_owner_index"`), and that a `delete_owner_index` failure fails only that step.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_message_search.py -q`
Expected: FAIL — `ImportError: cannot import name 'message_search'`.

- [ ] **Step 3: Implement**

`core/config.py`: `SEARCH_ENDPOINT: str = ""  # OpenSearch Serverless collection endpoint; empty disables message search`

`core/services/messaging.py`: add `"SEARCH_UNAVAILABLE": 503,` to `API_ERROR_STATUS`. Next to `_room_summaries`:

```python
async def _visible_room_metas(owner_id: str, user_id: str) -> list[dict]:
    """Rooms the GSI says the caller belongs to, minus tombstoned rooms and Meet
    rooms. Discovery only: every caller still rechecks the strong ``MEMBER#`` row."""
    return [
        row["meta"]
        for row in await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
        # ``hidden``: a Google Meet's room -- the meeting's record, never a place people go.
        if not row["meta"].get("deletedAt") and not row["meta"].get("hidden")
    ]


async def searchable_rooms(owner_id: str, user_id: str) -> dict[str, str]:
    """Room id -> the room's OWNER (whose search index holds it), for every room
    the caller can read right now -- the same membership rule as ``require_room_member``."""
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def check(meta: dict) -> dict | None:
        async with limit:
            member = await messaging_repo.get_member(meta["roomId"], "human", user_id)
        if member is not None and messaging_repo.member_home(member, meta["ownerId"]) == owner_id:
            return meta
        return None

    checked = await asyncio.gather(*(check(meta) for meta in await _visible_room_metas(owner_id, user_id)))
    return {meta["roomId"]: meta["ownerId"] for meta in checked if meta}
```

In `_room_summaries`, replace the `discovered = [...]` comprehension with `discovered = await _visible_room_metas(owner_id, user_id)` and change `summarise(row)` to take the meta directly (`meta = row["meta"]` → parameter `meta`). Behaviour is unchanged.

`core/services/message_search.py`:

```python
"""Cross-room message search over OpenSearch Serverless.

Access is decided HERE, from DynamoDB, on every request: the caller's current
rooms become both the index list and a ``terms`` filter, so the index never
decides who sees what. The query travels in a POST body and is never logged."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session
from pydantic import Field

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.services import messaging
from core.services.messaging import ApiProblem, ContractModel

logger = logging.getLogger(__name__)

MARK_START, MARK_END = "\u0001", "\u0002"
_SEARCH_TIMEOUT_S = 25.0  # under API Gateway's 30 s; a cold start can take 10–30 s
_SOURCE = ["messageId", "roomId", "messageSeq", "threadRootMessageId", "authorName", "authorKind", "createdAt", "text"]
_OPERATORS = set('"-+|*()~')

_credentials = None
_client: httpx.AsyncClient | None = None


class MessageSearchRequest(ContractModel):
    q: str = Field(min_length=1, max_length=200)
    room_id: uuid.UUID | None = None
    limit: int = Field(default=30, ge=1, le=50)


class MessageSearchHit(ContractModel):
    message_id: str
    room_id: uuid.UUID
    message_seq: int
    thread_root_message_id: str | None
    author_name: str
    author_kind: str
    created_at: str
    snippet: str


class MessageSearchResponse(ContractModel):
    hits: list[MessageSearchHit]


def index_name(owner_id: str) -> str:
    # MUST match apps/infra/lambda/messaging-search-indexer/index.py:index_name.
    return "messages-" + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def build_query(q: str, room_ids: list[str], limit: int) -> dict:
    fields = ["text^3", "authorName"]
    # Phrases, -exclusions, prefix*; never throws on bad syntax.
    should: list[dict] = [{"simple_query_string": {"query": q, "fields": fields, "default_operator": "and", "boost": 2}}]
    if not _OPERATORS & set(q):
        # Typo tolerance -- only when the person typed no operators, or this OR
        # would match what a phrase or an exclusion asked to leave out.
        should.append({"multi_match": {"query": q, "fields": fields, "operator": "and", "fuzziness": "AUTO"}})
    return {
        "size": limit,
        "_source": _SOURCE,
        "query": {"function_score": {
            "query": {"bool": {"filter": [{"terms": {"roomId": room_ids}}], "should": should, "minimum_should_match": 1}},
            # Recency nudges, never buries: the factor runs 0.5–1.5.
            "functions": [
                {"gauss": {"createdAt": {"origin": "now", "offset": "7d", "scale": "90d", "decay": 0.5}}, "weight": 1},
                {"weight": 0.5},
            ],
            "score_mode": "sum",
            "boost_mode": "multiply",
        }},
        "highlight": {
            "pre_tags": [MARK_START],
            "post_tags": [MARK_END],
            "fields": {"text": {"number_of_fragments": 1, "fragment_size": 160, "no_match_size": 160}},
        },
    }


async def _request(method: str, path: str, body: dict | None = None, timeout: float = _SEARCH_TIMEOUT_S) -> httpx.Response:
    # Credentials and the client are cached like live_events.LiveEventPublisher:
    # a fresh botocore session per call is a blocking credentials fetch on ECS.
    global _credentials, _client
    if _credentials is None:
        _credentials = get_session().get_credentials()
    if _client is None:
        _client = httpx.AsyncClient()
    data = json.dumps(body).encode() if body is not None else b""
    url = f"{settings.SEARCH_ENDPOINT.rstrip('/')}{path}"
    signed = AWSRequest(
        method=method, url=url, data=data,
        headers={"content-type": "application/json", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()},
    )
    SigV4Auth(_credentials.get_frozen_credentials(), "aoss", settings.AWS_REGION).add_auth(signed)
    return await _client.request(method, url, content=data, headers=dict(signed.headers.items()), timeout=timeout)


def _unavailable() -> ApiProblem:
    return ApiProblem("SEARCH_UNAVAILABLE", "Search is starting up. Try again in a few seconds.", retryable=True, retry_after=3)


async def search(auth: AuthContext, request: MessageSearchRequest) -> MessageSearchResponse:
    if not settings.SEARCH_ENDPOINT:
        raise ApiProblem("SEARCH_UNAVAILABLE", "Message search is not available yet.")
    q = request.q.strip()
    rooms = await messaging.searchable_rooms(resolve_owner_id(auth), auth.user_id)
    if request.room_id is not None:
        room_id = str(request.room_id)
        if room_id not in rooms:
            raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        rooms = {room_id: rooms[room_id]}
    if not q or not rooms:
        return MessageSearchResponse(hits=[])
    indexes = ",".join(sorted({index_name(owner) for owner in rooms.values()}))
    try:
        response = await _request("POST", f"/{indexes}/_search?ignore_unavailable=true", build_query(q, list(rooms), request.limit))
    except httpx.TimeoutException as exc:
        raise _unavailable() from exc
    if response.status_code >= 500:
        logger.warning("message search unavailable status=%s", response.status_code)
        raise _unavailable()
    if response.status_code != 200:
        logger.error("message search rejected status=%s", response.status_code)
        raise ApiProblem("INTERNAL_ERROR", "Search failed.")
    hits = []
    for hit in response.json().get("hits", {}).get("hits", []):
        source = hit["_source"]
        snippet = (hit.get("highlight", {}).get("text") or [source.get("text", "")[:160]])[0]
        hits.append(MessageSearchHit(
            message_id=source["messageId"], room_id=source["roomId"], message_seq=source["messageSeq"],
            thread_root_message_id=source.get("threadRootMessageId"), author_name=source.get("authorName", ""),
            author_kind=source.get("authorKind", ""), created_at=source["createdAt"], snippet=snippet,
        ))
    return MessageSearchResponse(hits=hits)


async def warm(auth: AuthContext) -> None:
    """Wake search capacity when someone opens a search box. Any request wakes a
    scaled-to-zero collection, so this does not wait for the answer."""
    if not settings.SEARCH_ENDPOINT:
        return
    try:
        await _request("GET", f"/{index_name(resolve_owner_id(auth))}/_count", timeout=2.0)
    except httpx.HTTPError:
        pass


async def delete_owner_index(owner_id: str) -> str:
    """Drop a wiped owner's index. Runs AFTER the room partitions are gone; an
    index job still in flight then re-reads a missing row and deletes, not re-adds."""
    if not settings.SEARCH_ENDPOINT:
        return "search off"
    try:
        response = await _request("DELETE", f"/{index_name(owner_id)}", timeout=60.0)
    except httpx.TimeoutException as exc:
        raise RuntimeError("search index delete timed out (cold collection); re-run the wipe") from exc
    if response.status_code not in (200, 404):
        raise RuntimeError(f"search index delete failed status={response.status_code}")
    return "dropped" if response.status_code == 200 else "no index"
```

`routers/messaging.py` (import `message_search` from `core.services`; confirm `Response` is imported):

```python
@router.post("/messaging/search", response_model=message_search.MessageSearchResponse)
async def search_messages(body: message_search.MessageSearchRequest, auth: AuthContext = Depends(get_current_user)):
    # POST, not GET: a query string would land in the access log.
    return await message_search.search(auth, body)


@router.post("/messaging/search/warm", status_code=204)
async def warm_message_search(auth: AuthContext = Depends(get_current_user)) -> Response:
    await message_search.warm(auth)
    return Response(status_code=204)
```

`account_wipe.py`, right after the messaging `_step(...)` call:

```python
    async def _s9a3() -> str:
        return await message_search.delete_owner_index(owner_id)

    await _step(steps, system="search", action="drop_owner_index", fn=_s9a3)
```

`workspace_wipe.py`: add `async def _search_run(snap: WorkspaceSnapshot) -> str: return await message_search.delete_owner_index(snap.org_id)` next to `_messaging_run`, and right after the `"messaging"` `WorkspaceStepDef`:

```python
    WorkspaceStepDef(
        "search",
        "drop_owner_index",
        LAYER_MESSAGING,
        lambda snap: (True, "the workspace's message search index"),
        _search_run,
    ),
```

Both files: `from core.services import message_search`.

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_message_search.py tests/unit/services/test_account_wipe.py tests/unit/services/test_workspace_wipe.py tests/unit/services/test_wipe_never_deprovisions.py -q` → PASS. If a test pins the exact workspace-wipe step list or count, add the new step there.
Run: `cd apps/backend && uv run pytest tests/unit/routers -q -k messaging` → PASS (the router module still imports and existing room tests are unaffected by the `_room_summaries` refactor).
Run: `cd apps/backend && uv run ruff check core/services/message_search.py core/services/messaging.py routers/messaging.py core/services/account_wipe.py core/services/workspace_wipe.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core apps/backend/routers/messaging.py apps/backend/tests/unit/services
git commit -m "feat(messaging): message search API, warm-up and wipe cleanup"
```

---

### Task 5: Re-runnable reindex script

**Files:**
- Create: `apps/backend/scripts/reindex_messages.py`
- Test: `apps/backend/tests/unit/scripts/test_reindex_messages.py` (the directory exists)

**Interfaces:**
- Consumes: the index queue (`aws sqs get-queue-url --queue-name lightbulb-<env>-message-search-index`); SQS body format from Task 2.
- Produces: CLI `uv run python scripts/reindex_messages.py --env dev --queue-url <url> [--owner org_x] [--profile isol8-admin]`.

It stays in the repo (re-runnable ops tool): standby replicas are off, so it is the index rebuild path. A message row's `ownerId` is the room owner (room-scoped writes key on the room owner — `update_message` in `core/services/messaging.py`).

- [ ] **Step 1: Write the failing test**

```python
from scripts import reindex_messages


def test_jobs_from_rows_keeps_messages_and_points_at_the_room_owner():
    rows = [
        {"pk": "ROOM#r1", "sk": "MSG#00000000000000000001", "roomId": "r1", "ownerId": "org_a"},
        {"pk": "ROOM#r1", "sk": "META", "roomId": "r1", "ownerId": "org_a"},
    ]
    assert reindex_messages.jobs_from_rows(rows) == [{"ownerId": "org_a", "roomId": "r1", "sk": "MSG#00000000000000000001"}]
```

- [ ] **Step 2:** `cd apps/backend && uv run pytest tests/unit/scripts/test_reindex_messages.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
"""Re-enqueue messages for the search indexer (backfill, or rebuild an index).

Re-runnable: the indexer re-reads each row, so repeating a run is harmless.

    aws sqs get-queue-url --queue-name lightbulb-<env>-message-search-index --profile isol8-admin
    uv run python scripts/reindex_messages.py --env dev --queue-url <url> [--owner org_x]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable

import boto3
from boto3.dynamodb.conditions import Attr


def jobs_from_rows(rows: Iterable[dict]) -> list[dict]:
    return [
        {"ownerId": row["ownerId"], "roomId": row["roomId"], "sk": row["sk"]}
        for row in rows
        if str(row.get("sk", "")).startswith("MSG#") and row.get("ownerId") and row.get("roomId")
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--owner")
    parser.add_argument("--profile", default="isol8-admin")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    table = session.resource("dynamodb").Table(f"lightbulb-{args.env}-messaging")
    sqs = session.client("sqs")
    condition = Attr("sk").begins_with("MSG#")
    if args.owner:
        condition = condition & Attr("ownerId").eq(args.owner)
    scan = {"FilterExpression": condition, "ProjectionExpression": "pk, sk, roomId, ownerId"}
    sent = 0
    while True:
        page = table.scan(**scan)
        jobs = jobs_from_rows(page["Items"])
        for start in range(0, len(jobs), 10):  # SendMessageBatch takes at most 10
            batch = jobs[start : start + 10]
            response = sqs.send_message_batch(
                QueueUrl=args.queue_url,
                Entries=[{"Id": str(i), "MessageBody": json.dumps(job, separators=(",", ":"))} for i, job in enumerate(batch)],
            )
            if response.get("Failed"):
                raise SystemExit(f"SQS rejected {len(response['Failed'])} job(s); re-run to retry")
            sent += len(batch)
        if "LastEvaluatedKey" not in page:
            break
        scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(f"enqueued {sent} message(s)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4:** test → PASS.
- [ ] **Step 5: Commit, push PR A, watch CI**

```bash
git add apps/backend/scripts/reindex_messages.py apps/backend/tests/unit/scripts/test_reindex_messages.py
git commit -m "feat(messaging): re-runnable message search reindex script"
git push -u origin feat/message-search
gh pr create --repo TheLightbulbCompany/lightbulb --title "feat(messaging): message search backend (dev)" --body "<summary; security notes incl. OAuth/MCP tokens can search like read_messages; test plan; Claude Code attribution line>"
```

Watch: `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60` (backend CI can take ~70 min; use a 2-hour watch). Check Codex on all four surfaces. Merge only on green + Codex reviewed. Backend changes touch app startup imports (router), so let `backend.yml`'s full suite finish.

---

### Task 6: Dev acceptance and the cost gate (owner decision)

**Files:** none. Output: a PR comment with receipts and a cost estimate; the owner approves or rejects prod.

- [ ] **Step 1: Confirm the deploy** — `aws opensearchserverless batch-get-collection --names lightbulb-dev-messages --profile isol8-admin --region us-east-1` → `status: ACTIVE`.
- [ ] **Step 2: Backfill dev** — `aws sqs get-queue-url --queue-name lightbulb-dev-message-search-index …` then `uv run python scripts/reindex_messages.py --env dev --queue-url <url>`; DLQ stays empty.
- [ ] **Step 3: Verify the unknowns, record each on the PR**
  1. NEXTGEN group with min 0 OCU, inline `encryptionConfig`, and the collection inheriting standby from the group all deployed (else: explicit encryption `CfnSecurityPolicy`, or `standbyReplicas: "DISABLED"` on the collection, in a follow-up commit).
  2. Custom document ids on a SEARCH collection (indexer logs show no 4xx).
  3. `ignore_unavailable=true` accepted on `_search` (a workspace with no index returns 200).
  4. Cold start: after 15 idle minutes, time `POST /messaging/search/warm` + a search until 200.
  5. Whether search sleeps while indexing is busy (`AWS/AOSS` `SearchOCU` vs `IndexingOCU`).
  6. Index quota per collection (Service Quotas, "OpenSearch Serverless") vs prod workspace count.
  7. Security: a signed request from outside the VPC is refused; a dev account removed from a room gets no hits from it; the backend access log line for a search contains no query text.
  8. Ranking sanity: an exact match from months ago still appears for its words; `"exact phrase"` and `-word` behave.
- [ ] **Step 4: Cost estimate** — `IndexingOCU` per active hour in dev × prod's write duty cycle (prod `ConsumedWriteCapacityUnits` on `lightbulb-prod-messaging`: hours per day with writes) × $0.24/OCU-hr, plus the VPC endpoint's hourly charge from the bill. Post the number; **stop until the owner approves**.

---

## PR B — prod

### Task 7: Turn prod on and backfill

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts` (delete `const searchEnabled = env === "dev";`, the `if/else`, `let indexDlq` + the alarm's `if`, and the `ponytail:` line — the block always runs)
- Modify: `apps/infra/test/api-stack.test.ts` (replace the "prod has no search resources" test; add the prod queue names if a prod queue-list test exists)
- Modify: `.github/workflows/deploy.yml` (regenerated)

- [ ] **Step 1: Change the prod test to expect search**

```ts
  test("prod has the same search resources", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::OpenSearchServerless::Collection", { Name: "lightbulb-prod-messages", Type: "SEARCH" });
  });
```

- [ ] **Step 2:** `cd apps/infra && npx jest test/api-stack.test.ts` → FAIL.
- [ ] **Step 3:** Remove the env condition (keep the block body), `npx cdk synth`, re-run → PASS.
- [ ] **Step 4:** Commit, PR, CI, Codex, merge. A human dispatches prod (`gh workflow run deploy.yml --ref main`, only when `gh run list --workflow deploy.yml … --json status` shows 0 in flight).
- [ ] **Step 5:** After prod lands: `reindex_messages.py --env prod --queue-url <prod url>`; DLQ empty; one signed-in prod search returns hits.

---

## PR C — UI (merge only after Task 7 Step 5)

### Task 8: Shared client methods

**Files:**
- Modify: `packages/shared/src/messaging/types.ts` (after `MessagePage`)
- Modify: `packages/shared/src/messaging/client.ts` (interface ~line 128; implementation next to `deleteRoom` ~line 260)
- Test: `packages/shared/src/messaging/client.test.ts`

**Interfaces:**
- Produces:
  - `interface MessageSearchHit { messageId: string; roomId: string; messageSeq: number; threadRootMessageId: string | null; authorName: string; authorKind: string; createdAt: string; snippet: string }`
  - `interface MessageSearchResponse { hits: MessageSearchHit[] }`
  - `searchMessages(query: string, options?: { roomId?: string; limit?: number }): Promise<MessageSearchResponse>`
  - `warmSearch(): Promise<void>`

- [ ] **Step 1: Failing test** (in `describe("messaging HTTP client", …)`):

```ts
  it("searches messages in a POST body and warms search", async () => {
    const { client, methods } = setup();
    await client.searchMessages("deploy fix", { roomId, limit: 20 });
    expect(methods.post).toHaveBeenCalledWith("/messaging/search", { q: "deploy fix", roomId, limit: 20 });
    await client.searchMessages("deploy");
    expect(methods.post).toHaveBeenLastCalledWith("/messaging/search", { q: "deploy" });
    await client.warmSearch();
    expect(methods.post).toHaveBeenLastCalledWith("/messaging/search/warm", {});
  });
```

- [ ] **Step 2:** run it (from `packages/shared` if it has its own vitest config, else `cd apps/frontend && pnpm exec vitest run ../../packages/shared/src/messaging/client.test.ts`) → FAIL.
- [ ] **Step 3:** Types as above (export them wherever `MessagePage` is exported from the package barrel); interface lines; implementation:

```ts
    // POST: the query must not ride in a URL (access logs).
    searchMessages: (query, options) => api.post("/messaging/search", {
      q: query,
      ...(options?.roomId ? { roomId: options.roomId } : {}),
      ...(options?.limit ? { limit: options.limit } : {}),
    }) as Promise<MessageSearchResponse>,
    async warmSearch() {
      await api.post("/messaging/search/warm", {});
    },
```

- [ ] **Step 4:** test → PASS.
- [ ] **Step 5:** `git commit -m "feat(shared): message search client methods"`

### Task 9: One search hook, snippets, and the ⌘K "Messages" group

**Files:**
- Create: `apps/frontend/src/components/messaging/searchSnippet.tsx`
- Create: `apps/frontend/src/components/messaging/useMessageSearch.ts`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (expose the existing `openNotificationTarget` on the context: interface ~line 34, provider value ~line 850)
- Modify: `apps/frontend/src/components/teams/command-palette/CommandPalette.tsx`
- Modify: `apps/frontend/src/components/workspace/WorkspaceHeader.tsx` (search button `aria-label`)
- Test: `apps/frontend/src/components/messaging/__tests__/searchSnippet.test.tsx`, `apps/frontend/src/components/messaging/__tests__/useMessageSearch.test.tsx`

**Interfaces:**
- Consumes: Task 8 via `useLiveEvents().messaging` (`@/hooks/useLiveEvents`).
- Produces:
  - `snippetParts(snippet: string): { text: string; match: boolean }[]`, `plainSnippet(snippet: string): string`, `<SearchSnippet snippet={string} />` (React text nodes and `<mark>` only — never `dangerouslySetInnerHTML`).
  - `type SearchState = "idle" | "loading" | "warming" | "error" | "done"`; `useMessageSearch(query: string, options: { enabled: boolean; roomId?: string; limit: number; minLength?: number }): { hits: MessageSearchHit[]; state: SearchState }` — warms when enabled, debounces 250 ms, retries once 3 s after a 503, never sets state synchronously in an effect.
  - Context `openNotificationTarget(roomId: string, threadId: string | null, messageId?: string): void`.

- [ ] **Step 1: Failing tests**

`searchSnippet.test.tsx`:

```tsx
// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { plainSnippet, SearchSnippet, snippetParts } from "../searchSnippet";

describe("search snippets", () => {
  it("splits on the server's match marks", () => {
    expect(snippetParts("the \u0001deploy\u0002 fix")).toEqual([
      { text: "the ", match: false }, { text: "deploy", match: true }, { text: " fix", match: false },
    ]);
    expect(plainSnippet("the \u0001deploy\u0002 fix")).toBe("the deploy fix");
  });

  it("renders message HTML as text, never markup", () => {
    const { container } = render(<SearchSnippet snippet={"<img src=x onerror=alert(1)> \u0001hi\u0002"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("mark")?.textContent).toBe("hi");
  });
});
```

`useMessageSearch.test.tsx`:

```tsx
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchMessages: vi.fn(), warmSearch: vi.fn() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }) }));

import { useMessageSearch } from "../useMessageSearch";

const hit = { messageId: "m1", roomId: "r1", messageSeq: 1, threadRootMessageId: null, authorName: "Ann",
  authorKind: "human", createdAt: "2026-10-04T00:00:00Z", snippet: "the \u0001deploy\u0002 fix" };

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("useMessageSearch", () => {
  it("warms once when enabled, then searches after a pause", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockResolvedValue({ hits: [hit] });
    const { result, rerender } = renderHook(({ q }) => useMessageSearch(q, { enabled: true, limit: 8, minLength: 2 }), { initialProps: { q: "d" } });
    expect(messaging.warmSearch).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe("idle");
    rerender({ q: "deploy" });
    expect(result.current.state).toBe("loading");
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(messaging.searchMessages).toHaveBeenCalledWith("deploy", { roomId: undefined, limit: 8 });
    expect(result.current).toEqual({ hits: [hit], state: "done" });
  });

  it("reports warming on a 503 and retries once", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce({ hits: [hit] });
    const { result } = renderHook(() => useMessageSearch("deploy", { enabled: true, roomId: "r1", limit: 50 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(result.current.state).toBe("warming");
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(messaging.searchMessages).toHaveBeenCalledTimes(2);
    expect(result.current.state).toBe("done");
  });
});
```

- [ ] **Step 2:** `cd apps/frontend && pnpm exec vitest run src/components/messaging/__tests__/searchSnippet.test.tsx src/components/messaging/__tests__/useMessageSearch.test.tsx` → FAIL (modules missing).

- [ ] **Step 3: Implement**

`searchSnippet.tsx`:

```tsx
// The search API marks matches with \u0001…\u0002 instead of HTML, so a
// message's own text can never become markup here.
export function snippetParts(snippet: string): { text: string; match: boolean }[] {
  return snippet.split(/(\u0001[^\u0002]*\u0002)/).filter(Boolean).map((part) => part.startsWith("\u0001")
    ? { text: part.slice(1, -1), match: true }
    : { text: part, match: false });
}

export const plainSnippet = (snippet: string) => snippet.replace(/[\u0001\u0002]/g, "");

export function SearchSnippet({ snippet }: { snippet: string }) {
  return <>{snippetParts(snippet).map((part, index) => part.match ? <mark key={index}>{part.text}</mark> : part.text)}</>;
}
```

`useMessageSearch.ts`:

```ts
"use client";

import { useEffect, useState } from "react";
import type { MessageSearchHit } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export type SearchState = "idle" | "loading" | "warming" | "error" | "done";

const DEBOUNCE_MS = 250;
const RETRY_MS = 3_000;

/** One search box's results: warm on open, debounce, one retry while search wakes. */
export function useMessageSearch(
  query: string,
  { enabled, roomId, limit, minLength = 1 }: { enabled: boolean; roomId?: string; limit: number; minLength?: number },
): { hits: MessageSearchHit[]; state: SearchState } {
  const { messaging } = useLiveEvents();
  const term = query.trim();
  const active = enabled && term.length >= minLength;
  const key = `${roomId ?? ""}\u0000${term}`;
  const [result, setResult] = useState<{ key: string; hits: MessageSearchHit[]; state: SearchState }>({ key: "", hits: [], state: "idle" });

  useEffect(() => {
    if (enabled) void messaging.warmSearch().catch(() => {});
  }, [enabled, messaging]);

  useEffect(() => {
    if (!active) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const run = (retried: boolean) => messaging.searchMessages(term, { roomId, limit })
      .then((response) => { if (live) setResult({ key, hits: response.hits, state: "done" }); })
      .catch((error: { status?: number }) => {
        if (!live) return;
        const warming = error?.status === 503;
        setResult({ key, hits: [], state: warming ? "warming" : "error" });
        if (warming && !retried) retry = setTimeout(() => void run(true), RETRY_MS);
      });
    const timer = setTimeout(() => void run(false), DEBOUNCE_MS);
    return () => { live = false; clearTimeout(timer); clearTimeout(retry); };
  }, [active, key, term, roomId, limit, messaging]);

  if (!active) return { hits: [], state: "idle" };
  return result.key === key ? { hits: result.hits, state: result.state } : { hits: [], state: "loading" };
}
```

`MessagingWorkspaceContext.tsx`: add `openNotificationTarget: (roomId: string, threadId: string | null, messageId?: string) => void;` to the context interface and `openNotificationTarget,` to the provider value. Nothing else changes: it already opens the room, opens the thread for a reply, scrolls to a room-level message (loading history until found) and activates Messages.

`CommandPalette.tsx`:
- imports: `useMessageSearch` from `@/components/messaging/useMessageSearch`; `plainSnippet` from `@/components/messaging/searchSnippet`; `useMessagingWorkspaceOptional` from `@/components/messaging/MessagingWorkspaceContext`; `roomDisplayName` from `@lightbulb/shared`.
- `kind: "conversation" | "message" | "nav" | "agent" | "issue" | "project";`
- in `CommandPaletteContent`:

```tsx
  const workspace = useMessagingWorkspaceOptional();
  const messageSearch = useMessageSearch(query, { enabled: open && !!workspace, limit: 8, minLength: 2 });
  const messageHits = useMemo(() => messageSearch.hits.map((hit) => {
    const room = workspace?.directoryProps?.rooms.find((candidate) => candidate.roomId === hit.roomId);
    return {
      id: `message:${hit.messageId}`,
      label: plainSnippet(hit.snippet),
      sublabel: room ? `${hit.authorName} · ${roomDisplayName(room, room.participants)}` : hit.authorName,
      run: () => workspace?.openNotificationTarget(hit.roomId, hit.threadRootMessageId, hit.messageId),
    };
  }), [messageSearch.hits, workspace]);
  const warming = messageSearch.state === "warming";
```

- in `flatItems`, right after the conversations loop: `messageHits.forEach((m) => items.push({ kind: "message", id: m.id, label: m.label, path: "", run: m.run }));` (add `messageHits` to the memo deps).
- the empty branch: render `"Search is starting up…"` instead of `"No results."` when `warming`.
- after the Conversations `<Group>`:

```tsx
              {messageHits.length > 0 && (
                <Group label="Messages">
                  {messageHits.map((m) => {
                    const idx = flatItems.findIndex((it) => it.kind === "message" && it.id === m.id);
                    return (
                      <Row key={m.id} label={m.label} sublabel={m.sublabel} selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)} onClick={() => select(flatItems[idx])} />
                    );
                  })}
                </Group>
              )}
```

- placeholder: `"Search messages, agents, issues, projects, or jump to..."`. `WorkspaceHeader.tsx` search button `aria-label`: `"Search messages, agents, issues, and projects"` (update any test that asserts the old label).

- [ ] **Step 4:** the two new tests → PASS; `cd apps/frontend && pnpm exec vitest related --run src/components/messaging/MessagingWorkspaceContext.tsx src/components/teams/command-palette/CommandPalette.tsx src/components/workspace/WorkspaceHeader.tsx src/components/messaging/useMessageSearch.ts` → PASS (palette tests that mock `useLiveEvents` need `messaging: { searchMessages: vi.fn().mockResolvedValue({ hits: [] }), warmSearch: vi.fn().mockResolvedValue(undefined) }`); `pnpm exec eslint` on the changed files → clean.
- [ ] **Step 5:** `git commit -m "feat(messaging): search messages from ⌘K"`

### Task 10: In-room search uses the server

**Files:**
- Modify (rewrite): `apps/frontend/src/components/messaging/RoomMessageSearch.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (the `<RoomMessageSearch …>` mount and its `onSelect` body, ~line 687-704; a new optional prop `onJumpToMessage`)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (`roomProps.onJumpToMessage`)
- Test (rewrite): `apps/frontend/src/components/messaging/__tests__/RoomMessageSearch.test.tsx`

**Interfaces:**
- Consumes: `useMessageSearch`, `SearchSnippet` (Task 9).
- Produces: `RoomMessageSearch({ roomId, onSelect }: { roomId: string; onSelect: (hit: MessageSearchHit) => void })`; `roomProps.onJumpToMessage: (messageId: string) => void` — sets the provider's permalink `target` for the active room, which pages history until the message is loaded, then scrolls and highlights it.

Why both paths: server hits can be messages the client never loaded. A main-list hit therefore goes through the provider's `target` (the permalink path, which pages history). A thread reply keeps RoomPanel's shipped thread branch (`setSelection` + `setSearchTarget`), which opens the thread and scrolls/highlights the reply (effect at `RoomPanel.tsx:507-513`). The `searchTarget` state stays — `onOpenMessage` (~line 791) also writes it.

- [ ] **Step 1: Failing test** (replace the file):

```tsx
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchMessages: vi.fn(), warmSearch: vi.fn() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { RoomMessageSearch } from "../RoomMessageSearch";

const hit = { messageId: "m1", roomId: "r1", messageSeq: 3, threadRootMessageId: null, authorName: "Ann",
  authorKind: "human", createdAt: "2026-10-04T00:00:00Z", snippet: "the \u0001deploy\u0002 fix" };

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

async function typeQuery(value: string) {
  fireEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search messages" }), { target: { value } });
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
}

describe("RoomMessageSearch", () => {
  it("warms on open, searches this room, and opens a hit", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockResolvedValue({ hits: [hit] });
    const onSelect = vi.fn();
    render(<RoomMessageSearch roomId="r1" onSelect={onSelect} />);
    await typeQuery("deploy");
    expect(messaging.warmSearch).toHaveBeenCalledTimes(1);
    expect(messaging.searchMessages).toHaveBeenCalledWith("deploy", { roomId: "r1", limit: 50 });
    expect(screen.getByText("deploy").tagName).toBe("MARK");
    fireEvent.click(screen.getByRole("button", { name: /Ann/ }));
    expect(onSelect).not.toHaveBeenCalled(); // fires on close-auto-focus, like before
  });

  it("says search is starting up on a 503 and retries once", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce({ hits: [hit] });
    render(<RoomMessageSearch roomId="r1" onSelect={vi.fn()} />);
    await typeQuery("deploy");
    expect(screen.getByRole("status").textContent).toMatch(/starting up/i);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(messaging.searchMessages).toHaveBeenCalledTimes(2);
    expect(screen.getByText("deploy").tagName).toBe("MARK");
  });
});
```

(If the existing dialog tests show `onCloseAutoFocus` fires in jsdom on close, assert `onSelect` was called with `hit` instead; keep whichever matches the shipped behaviour.)

- [ ] **Step 2:** `cd apps/frontend && pnpm exec vitest run src/components/messaging/__tests__/RoomMessageSearch.test.tsx` → FAIL.

- [ ] **Step 3: Implement** `RoomMessageSearch.tsx`:

```tsx
"use client";

import { useRef, useState } from "react";
import { Search } from "lucide-react";
import type { MessageSearchHit } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { capture } from "@/lib/analytics";
import { SearchSnippet } from "./searchSnippet";
import { useMessageSearch } from "./useMessageSearch";

export function RoomMessageSearch({ roomId, onSelect }: { roomId: string; onSelect: (hit: MessageSearchHit) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const { hits, state } = useMessageSearch(query, { enabled: open, roomId, limit: 50 });
  const selected = useRef<MessageSearchHit | null>(null);
  const term = query.trim();

  const reportSearchCompletion = () => {
    if (!term) return;
    capture("message_search_used", { result_count_bucket: hits.length === 0 ? "0" : hits.length <= 5 ? "1-5" : "6+" });
  };

  return <Dialog open={open} onOpenChange={(next) => {
    if (!next) reportSearchCompletion();
    setOpen(next);
    if (next) { setQuery(""); selected.current = null; }
  }}>
    <DialogTrigger asChild><Button type="button" variant="ghost" aria-label="Search this conversation"><Search size={18} aria-hidden="true" /><span className="hidden sm:inline">Search</span></Button></DialogTrigger>
    <DialogContent showCloseButton className="w-[calc(100%_-_2rem)] max-w-xl shadow-none" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => {
      if (!selected.current) return;
      event.preventDefault();
      onSelect(selected.current);
      selected.current = null;
    }}>
      <DialogTitle className="pr-8">Search this conversation</DialogTitle>
      <DialogDescription>Find messages and replies in this conversation.</DialogDescription>
      <Input type="search" aria-label="Search messages" placeholder="Search messages…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div role="status" className="text-sm text-muted-foreground">
        {state === "loading" ? "Searching…"
          : state === "warming" ? "Search is starting up. This can take a few seconds."
            : state === "error" ? "Search failed. Try again."
              : state === "done" ? `${hits.length} ${hits.length === 1 ? "message" : "messages"} found`
                : "Search the message text, including thread replies. Use quotes for an exact phrase."}
      </div>
      {state === "done" && hits.length === 0 && <p className="text-sm">No matching messages. Try a different word or phrase.</p>}
      {hits.length > 0 && <ul aria-label="Search results" className="max-h-[50dvh] overflow-y-auto -mx-2">
        {hits.map((hit) => {
          const validDate = !Number.isNaN(Date.parse(hit.createdAt));
          return <li key={hit.messageId}>
            <button type="button" className="w-full rounded-sm p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => { reportSearchCompletion(); selected.current = hit; setOpen(false); }}>
              <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm"><span className="font-semibold">{hit.authorName || "Unknown author"}</span>
                {validDate && <time dateTime={hit.createdAt} className="text-muted-foreground">{new Date(hit.createdAt).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
                {hit.threadRootMessageId && <span className="text-muted-foreground">Thread reply</span>}
              </span>
              <span className="mt-1 block whitespace-pre-wrap break-words text-sm"><SearchSnippet snippet={hit.snippet} /></span>
            </button>
          </li>;
        })}
      </ul>}
    </DialogContent>
  </Dialog>;
}
```

`MessagingWorkspaceContext.tsx`, in the `roomProps` object: `onJumpToMessage: (messageId: string) => setTarget({ roomId: rooms.activeRoomId!, messageId }),` (`roomProps` is only built while a room is active).

`RoomPanel.tsx`: add `onJumpToMessage?: (messageId: string) => void` to its props (destructure it like the other callbacks) and replace the mount + handler with:

```tsx
        search={<RoomMessageSearch roomId={roomId}
          onSelect={(hit) => {
            const root = hit.threadRootMessageId;
            // Otherwise the [selection] effect's else-branch focuses this stale trigger
            // (e.g. a "Reply in thread" button) once selection clears below, racing
            // RoomTimeline's own focus of the newly targeted main message.
            trigger.current = null;
            // Same race against the [contextCollapsed] effect below: if the room
            // details panel happens to be open, closing it here must not steal
            // focus back to the Details tab once RoomTimeline focuses this result.
            contextWasOpen.current = false;
            setContextCollapsed(true);
            if (root) {
              onSelectThread?.(root);
              setSelection({ kind: "thread", id: root });
              setSearchTarget({ messageId: hit.messageId, threadRootMessageId: root, token: ++searchTargetToken.current });
            } else {
              setSelection(null);
              // Server hits may not be loaded yet: the permalink target pages history until it is.
              onJumpToMessage?.(hit.messageId);
            }
          }} />}
```

- [ ] **Step 4:** test → PASS; `cd apps/frontend && pnpm exec vitest related --run src/components/messaging/RoomMessageSearch.tsx src/components/messaging/RoomPanel.tsx src/components/messaging/MessagingWorkspaceContext.tsx` → PASS (update RoomPanel tests that passed history props into search); `pnpm exec eslint` on changed files → clean; `pnpm exec tsc --noEmit -p .` → exit 0.
- [ ] **Step 5:** Commit, `git rm` this plan file (CLAUDE.md: planning docs are disposable), push PR C, CI + Codex, merge after Task 7 Step 5. Dev acceptance in Chrome (receipts on the PR):
  - ⌘K "deploy" finds a message from another room and opens it scrolled.
  - An in-room `"exact phrase"` search.
  - A misspelling still finds the message.
  - An old unloaded message is reached and highlighted.
  - A thread-reply hit opens the thread on the reply. Also confirm a reply whose root isn't loaded opens. If it doesn't, record it and call `onJumpToMessage(root)` before opening the thread.
  - A member removed from a room no longer sees its messages.
  - An edited message is found by its new wording only.
  - A deleted message disappears within a minute.
