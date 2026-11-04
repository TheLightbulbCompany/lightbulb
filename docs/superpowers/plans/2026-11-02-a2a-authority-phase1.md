# A2A Authority Extension, Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A hosted workspace agent (Friday) is reachable over A2A JSON-RPC at a Lightbulb URL, every task must carry our authority extension, a per-workspace rule document decides allow / deny / hold, held tasks are answered only by the resource's holder, and every decision is a signed, retrievable record.

**Architecture:** One new FastAPI router (`routers/a2a.py`) in front of three new modules: `a2a_authority` (mint, verify, hash, replay), `a2a_rules` (deterministic decision, holders, exceptions) and `a2a_tasks` (task state, dispatch, reply linking), backed by one repository (`a2a_repo`) that stores everything in the existing messaging DynamoDB table under an `A2A#{org}` partition. Dispatch reuses the existing paths: a DM room per (requester, agent) for Friday, and the person's computer link (`hosted_mcp._through_member`) for laptop sessions. Friday starts hops through a new Hermes plugin tool that calls a hook-token route.

**Tech Stack:** Python 3.12, FastAPI, `a2a-sdk` (types only), `cryptography` Ed25519 via `core/services/network_proof.py`, DynamoDB (moto in tests), pytest with the fixtures in `apps/backend/tests/conftest.py`.

**Spec:** `docs/superpowers/specs/2026-11-02-a2a-authority-extension-design.md` (approved 2 Nov 2026; phase 1 as cut in §2a).

## Global Constraints

- Extension URI: `https://thelightbulb.company/a2a/ext/authority/v1`. Carried in `Message.metadata[URI]` and listed in `Message.extensions`. Missing or unactivated → JSON-RPC error `-32008` (`ExtensionSupportRequiredError`). Present but invalid → `-32602` with the failed check named, never a secret.
- JSON-RPC v1.0 method names only: `SendMessage`, `GetTask`. `CancelTask` → `-32004`. Any other method → `-32601`.
- Authority object fields exactly as spec §4: `v, id, iat, exp, requester{person, org, verified_by}, chain[{agent, session, person, at}], on_behalf_of, operation{skill, resource, operands, operands_hash}, intent, policy`. `exp - iat <= 86400`. `intent` ≤ 2048 chars. `on_behalf_of` ≠ `requester.person` → deny.
- Signing: compact JWS, EdDSA, header `{"alg":"EdDSA","kid":network_proof.kid(),"typ":"lightbulb-a2a-authority"}` for the object and `"typ":"lightbulb-a2a-record"` for records, produced by `network_proof._sign`. Public key from `network_proof.public_key()`.
- Rule document fields exactly as spec §6 minus `when`: `revision, holders{resource: [person | "@session-person"]}, rules[{id, resource, skill, requesters, effect}], default`. Evaluation: any matching `deny` wins; else document order, first match; else `default`. Requester outside the org → `deny`. Holder == requester → `REJECTED`, rule `self`.
- Held task expiry 24h, computed at read time. Exception row TTL 24h. Replay id TTL 24h. A `revision` change cancels held tasks with reason `policy_changed`.
- Phase 1 auth: Clerk session JWT via `core.auth.get_current_user`; `ctx.org_id` must equal the path `{org}`. No new OAuth application.
- Laptop header in `text`: at most 512 bytes before the message: requester display, for whom, the first 200 characters of intent, the record URL.
- All work in this worktree's branch `feat/a2a-authority-design` or a fresh worktree from `origin/main`; draft PR; no merge or release without the founder; never read production data; never print `NETWORK_PROOF_KEY` or any token.
- Tests run from `apps/backend` with `uv run pytest <path> -v`. Add dependencies with `uv add`.

## Review Focus

1. A client sends the extension header but puts the authority request under a different metadata key, or lists a different URI in `Message.extensions`: expected `-32008`, not a crash. Pinned in Task 4.
2. A replayed authority `id` arrives at a different agent of the same org: expected rejection as replay (the id is org-scoped, not agent-scoped). Pinned in Task 1.
3. The holder answers a task that already expired on read: expected `409` with reason `expired`, no record of `allow_by_holder`. Pinned in Task 5.
4. Friday's reply lands in a DM room with two WORKING tasks: expected the oldest task completes first and the second stays WORKING (documented ceiling). Pinned in Task 4.
5. The origin person has no linked computer when a laptop session is the target: expected the task fails `undeliverable` immediately with a record, not a hang. Pinned in Task 6.

---

### Task 1: Dependency, extension object, mint, verify, hash, replay

**Files:**
- Modify: `apps/backend/pyproject.toml` (add `a2a-sdk` via `uv add a2a-sdk`; types only, no extras)
- Create: `apps/backend/core/services/a2a_authority.py`
- Create: `apps/backend/core/repositories/a2a_repo.py` (only `claim_authority_id` in this task; later tasks add functions)
- Test: `apps/backend/tests/unit/services/test_a2a_authority.py`

**Interfaces:**
- Consumes: `network_proof._sign(claims: dict, typ: str | None) -> str`, `network_proof.kid() -> str`, `network_proof.public_key() -> dict` (`core/services/network_proof.py:50-90`), `messaging_repo._table()` and the `expiresAt` TTL attribute (`core/repositories/messaging_repo.py:335`, `:2684`).
- Produces:
  - `URI = "https://thelightbulb.company/a2a/ext/authority/v1"`, `OBJECT_TYP = "lightbulb-a2a-authority"`, `RECORD_TYP = "lightbulb-a2a-record"`, `MAX_LIFE_S = 86_400`, `MAX_INTENT = 2048`
  - `class AuthorityRequest(ContractModel)`: `operation: Operation`, `intent: str`, `on_behalf_of: str | None = None`; `class Operation(ContractModel)`: `skill: str`, `resource: str`, `operands: dict`
  - `def operands_hash(operands: dict) -> str` → `"sha256:<hex>"` over `json.dumps(operands, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (`# ponytail: JCS-equivalent for JSON without floats; swap in an RFC 8785 canonicalizer when floats appear`)
  - `def mint(*, requester_person: str, org: str, verified_by: str, chain: list[dict], request: AuthorityRequest, now: int | None = None) -> tuple[str, dict]` → `(jws, claims)`; sets `v=1`, `id=str(uuid4())`, `iat`, `exp=iat+MAX_LIFE_S`, `policy=None`, fills `operands_hash`
  - `class AuthorityError(ValueError)` with `.check: str` in `{"signature","typ","exp","iat","shape","intent_length","on_behalf_of","origin","operands_hash"}`
  - `def verify(jws: str, *, now: int | None = None) -> dict` → claims or raises `AuthorityError`
  - `def sign_record(record: dict) -> str` (JWS with `RECORD_TYP`)
  - `a2a_repo.claim_authority_id(org: str, authority_id: str, now: int) -> bool` (conditional put `pk=A2A#{org}`, `sk=AUTH#{id}`, `expiresAt=now+86400`; `False` on `ConditionalCheckFailedException`)

- [ ] **Step 1: Add the dependency**

Run: `cd apps/backend && uv add a2a-sdk` then `uv run python -c "import a2a.types as t; print(t.AgentCard.__name__)"`
Expected: `AgentCard`

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/services/test_a2a_authority.py
from core.services import a2a_authority as a

REQ = a.AuthorityRequest(operation=a.Operation(skill="message", resource="agent:friday", operands={"text": "hi"}), intent="say hi")

def test_operands_hash_is_order_independent():
    assert a.operands_hash({"b": 1, "a": 2}) == a.operands_hash({"a": 2, "b": 1})
    assert a.operands_hash({"a": 1}).startswith("sha256:")

def test_mint_then_verify_roundtrip(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    jws, claims = a.mint(requester_person="user_1", org="org_1", verified_by="clerk-session", chain=[{"agent": None, "session": None, "person": "user_1", "at": 1}], request=REQ, now=1_000)
    out = a.verify(jws, now=1_001)
    assert out["id"] == claims["id"] and out["exp"] == 1_000 + a.MAX_LIFE_S
    assert out["operation"]["operands_hash"] == a.operands_hash({"text": "hi"})
    assert out["chain"][0]["person"] == "user_1" and out["policy"] is None

def test_verify_rejects_expired_and_tampered(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    jws, _ = a.mint(requester_person="user_1", org="org_1", verified_by="clerk-session", chain=[{"agent": None, "session": None, "person": "user_1", "at": 1}], request=REQ, now=1_000)
    with_exc = lambda f: __import__("pytest").raises(a.AuthorityError, match=f)
    with with_exc("exp"): a.verify(jws, now=1_000 + a.MAX_LIFE_S + 1)
    head, body, sig = jws.split(".")
    with with_exc("signature"): a.verify(f"{head}.{body}.{sig[:-2]}AA", now=1_001)

def test_verify_rejects_wrong_typ_and_long_intent(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    from core.services import network_proof
    proof = network_proof.mint("computer-key", "user_1", now=1_000)   # a sign-in proof, wrong typ
    with __import__("pytest").raises(a.AuthorityError, match="typ"): a.verify(proof, now=1_001)
    long = a.AuthorityRequest(operation=REQ.operation, intent="x" * (a.MAX_INTENT + 1))
    with __import__("pytest").raises(a.AuthorityError, match="intent_length"):
        a.mint(requester_person="user_1", org="org_1", verified_by="clerk-session", chain=[], request=long, now=1_000)

def test_claim_authority_id_is_org_scoped_and_once(repos):
    from core.repositories import a2a_repo
    import asyncio
    assert asyncio.run(a2a_repo.claim_authority_id("org_1", "id-1", 1_000)) is True
    assert asyncio.run(a2a_repo.claim_authority_id("org_1", "id-1", 1_000)) is False
    assert asyncio.run(a2a_repo.claim_authority_id("org_2", "id-1", 1_000)) is True
```

`repos` is a moto fixture modeled on `tests/unit/repositories/test_launch_teams_repo.py:21-35`, creating the messaging table with `pk`/`sk` keys and patching `core.config.settings.MESSAGING_TABLE`; put it in `tests/unit/services/conftest.py`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_a2a_authority.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.a2a_authority`

- [ ] **Step 4: Implement `a2a_authority.py` and `a2a_repo.claim_authority_id`**

`verify` decodes the compact JWS with the key from `network_proof.public_key()["key"]`, checks header `alg == "EdDSA"`, `kid == network_proof.kid()`, `typ == OBJECT_TYP`, then `iat <= now < exp`, `exp - iat <= MAX_LIFE_S`, shape (every field in Global Constraints present with the right type), `len(intent) <= MAX_INTENT`, `on_behalf_of in (None, requester.person)`, `chain[0].person == requester.person`, and recomputes `operands_hash`. Raise `AuthorityError(check)` with the first failing check as the message.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_a2a_authority.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/core/services/a2a_authority.py apps/backend/core/repositories/a2a_repo.py apps/backend/tests/unit/services/
git commit -m "feat(a2a): authority extension object, mint, verify, operands hash, replay claim"
```

---

### Task 2: Rules document and the decision

**Files:**
- Create: `apps/backend/core/services/a2a_rules.py`
- Modify: `apps/backend/core/repositories/a2a_repo.py` (add `get_rules`, `put_rules`, `put_exception`, `get_exception`)
- Test: `apps/backend/tests/unit/services/test_a2a_rules.py`

**Interfaces:**
- Consumes: Task 1's `repos` fixture pattern.
- Produces:
  - `class Rule(ContractModel)`: `id: str`, `resource: str`, `skill: str`, `requesters: list[str]`, `effect: Literal["allow","deny","hold"]`
  - `class Rules(ContractModel)`: `revision: str`, `holders: dict[str, list[str]]`, `rules: list[Rule]`, `default: Literal["allow","deny","hold"] = "hold"`
  - `def default_rules(org: str, admin_ids: list[str]) -> Rules` → holders `{"agent:*": admin_ids, "session:*": ["@session-person"], "room:*": admin_ids}`, rules `[Rule(id="r1", resource="agent:*", skill="message", requesters=[f"org:{org}"], effect="allow")]`, default `hold`
  - `@dataclass class Decision`: `effect: Literal["allow","deny","hold","reject"]`, `rule: str` (`rule id`, `"default"`, `"self"`, `"outside_org"`, or `"exception:<id>"`), `holders: list[str]`
  - `def decide(rules: Rules, *, org: str, requester_person: str, requester_org: str, chain_agents: list[str], resource: str, skill: str, session_person: str | None, exception: dict | None) -> Decision`
  - `def matches(pattern: str, value: str) -> bool` (exact, or `prefix:*`)
  - `a2a_repo.get_rules(org) -> dict | None`, `put_rules(org, rules: dict) -> None` (`sk=RULES`), `put_exception(org, resource, operands_hash, *, holder, task_id, now) -> str` (`sk=EXC#{resource}#{hash}`, `expiresAt=now+86400`, returns exception id), `get_exception(org, resource, operands_hash, now) -> dict | None` (None when `expiresAt <= now`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_a2a_rules.py
from core.services import a2a_rules as r
R = r.default_rules("org_1", ["user_admin"])
K = dict(org="org_1", requester_org="org_1", chain_agents=[], session_person=None, exception=None)

def test_in_org_message_to_agent_is_allowed_by_r1():
    d = r.decide(R, requester_person="user_x", resource="agent:friday", skill="message", **K)
    assert (d.effect, d.rule) == ("allow", "r1")

def test_outside_org_is_denied_before_any_rule():
    d = r.decide(R, requester_person="user_x", resource="agent:friday", skill="message", **{**K, "requester_org": "org_other"})
    assert (d.effect, d.rule) == ("deny", "outside_org")

def test_ask_session_falls_to_default_hold_with_session_person_as_holder():
    d = r.decide(R, requester_person="user_x", resource="session:abc", skill="ask_session", **{**K, "session_person": "user_owner"})
    assert (d.effect, d.rule, d.holders) == ("hold", "default", ["user_owner"])

def test_requester_who_is_the_holder_is_rejected_as_self():
    d = r.decide(R, requester_person="user_owner", resource="session:abc", skill="ask_session", **{**K, "session_person": "user_owner"})
    assert (d.effect, d.rule) == ("reject", "self")

def test_deny_rule_wins_over_an_earlier_allow():
    rules = R.model_copy(update={"rules": [*R.rules, r.Rule(id="d1", resource="agent:friday", skill="message", requesters=["user_x"], effect="deny")]})
    d = r.decide(rules, requester_person="user_x", resource="agent:friday", skill="message", **K)
    assert (d.effect, d.rule) == ("deny", "d1")

def test_exception_row_allows_within_window():
    d = r.decide(R, requester_person="user_x", resource="session:abc", skill="ask_session", **{**K, "session_person": "user_owner", "exception": {"id": "exc-9"}})
    assert (d.effect, d.rule) == ("allow", "exception:exc-9")

def test_exception_repo_roundtrip_and_expiry(repos):
    import asyncio
    from core.repositories import a2a_repo
    eid = asyncio.run(a2a_repo.put_exception("org_1", "session:abc", "sha256:aa", holder="user_owner", task_id="t1", now=1_000))
    assert asyncio.run(a2a_repo.get_exception("org_1", "session:abc", "sha256:aa", 1_001))["id"] == eid
    assert asyncio.run(a2a_repo.get_exception("org_1", "session:abc", "sha256:aa", 1_000 + 86_401)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_a2a_rules.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `a2a_rules.py` and the four repo functions**

Order inside `decide`: outside org → `deny/outside_org`; resolve holders (`@session-person` → `session_person`); requester in holders → `reject/self`; exception present → `allow/exception:<id>`; any matching `deny` rule → `deny/<id>`; first matching rule in order → its effect; else `default`. A requester matches `user_...` exactly, `org:<id>` when `requester_org == id`, `agent:<id>` when `id in chain_agents`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_a2a_rules.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/a2a_rules.py apps/backend/core/repositories/a2a_repo.py apps/backend/tests/unit/services/test_a2a_rules.py
git commit -m "feat(a2a): per-workspace rules, deterministic decision, holders, exceptions"
```

---

### Task 3: Task and record storage

**Files:**
- Modify: `apps/backend/core/repositories/a2a_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_a2a_repo.py`

**Interfaces:**
- Produces (all async, all under `pk=A2A#{org}`):
  - `put_task(org, task: dict) -> None` (`sk=TASK#{id}`, condition `attribute_not_exists(sk)`), `get_task(org, task_id) -> dict | None`, `update_task(org, task_id, *, state: str, extra: dict | None = None) -> None` (sets `state`, `updatedAt`, merges `extra`)
  - `append_record(org, task_id, record: dict) -> int` (`sk=RECORD#{task_id}#{n:06d}`, `n` = count of existing records + 1, condition `attribute_not_exists(sk)`), `list_records(org, task_id) -> list[dict]` (oldest first)
  - `list_held_tasks(org) -> list[dict]` (state `held`; a query with `begins_with(sk, "TASK#")` and a filter on `state`; `# ponytail: scan of the org's tasks; add a state GSI when an org holds thousands`)
  - Task dict shape: `{"id", "org", "agent", "state", "contextId", "authority_id", "authority": <jws>, "decision": {...}, "holders": [...], "dispatch_message_id", "room_id", "artifact": {...}|None, "createdAt", "updatedAt", "heldAt"|None, "requester_person"}`

- [ ] **Step 1: Write the failing tests**

```python
def test_put_get_update_task(repos):
    ...  # put a task, get it back, update state to "working" with extra {"room_id": "r1"}, assert both
def test_records_append_in_order_and_once(repos):
    ...  # append two records, list returns both oldest first, the sk numbers are 000001 and 000002
def test_list_held_tasks_filters_by_state(repos):
    ...  # three tasks, one held, list_held_tasks returns exactly that one
```

Write the bodies with the assertions above; use `asyncio.run` like Task 1.

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/unit/repositories/test_a2a_repo.py -v`, expected `AttributeError` on the missing functions.
- [ ] **Step 3: Implement the functions** using `messaging_repo._table()` and the condition pattern at `messaging_repo.py:535-546`.
- [ ] **Step 4: Run tests to verify they pass** — expected PASS (3 tests).
- [ ] **Step 5: Commit** — `git commit -m "feat(a2a): task and record storage in the messaging table"`

---

### Task 4: Agent Card, keys, JSON-RPC SendMessage and GetTask, dispatch to Friday, reply linking

**Files:**
- Create: `apps/backend/core/services/a2a_tasks.py`
- Create: `apps/backend/routers/a2a.py`
- Modify: `apps/backend/main.py` (add `from routers import a2a` and `app.include_router(a2a.router)` immediately before `app.mount("/", hosted_mcp.mount)` at `main.py:1199`)
- Modify: `apps/backend/routers/messaging.py:1353-1376` (`post_agent_message`: after `service.create_agent_message`, call `a2a_tasks.on_agent_reply(owner_id, room_id=str(body.room_id), agent_id=str(body.agent_id), message_id=receipt.message_id, text=body.text)`)
- Test: `apps/backend/tests/unit/routers/test_a2a.py`, `apps/backend/tests/integration/test_a2a_conformance.py`

**Interfaces:**
- Consumes: Tasks 1-3; `get_current_user` (`core/auth.py:211`), `AuthContext.is_org_admin`; `hosted_mcp.person_dm(auth, member_id, *, create_key)` (`hosted_mcp.py:601`); `messaging.create_human_message(auth, room_id, CreateMessageRequest(...), *, via=..., author_override=...)` (`messaging.py:4038`; `CreateMessageRequest` at `:601` needs `client_message_id`, `text`, `mentions=[]`, `session_mentions=[]`, `reply_to_message_id=None`, `thread_root_message_id=None`); `a2a.types` models from the SDK.
- Produces:
  - `a2a_tasks.agent_card(org: str, agent_id: str, agent_name: str, base_url: str) -> a2a.types.AgentCard` with `name`, `description`, `version="1"`, `supportedInterfaces=[AgentInterface(url=f"{base_url}/a2a/{org}/{agent_id}", protocolBinding="JSONRPC", protocolVersion="1.0", tenant=org)]`, `capabilities=AgentCapabilities(streaming=False, pushNotifications=False, extensions=[AgentExtension(uri=URI, required=True, description="Signed authority: who asked, through which agents, for whom, and exactly what.")])`, `securitySchemes` = one HTTP bearer scheme named `clerk`, `defaultInputModes=["text/plain"]`, `defaultOutputModes=["text/plain"]`, `skills=[AgentSkill(id="message", ...), AgentSkill(id="ask_session", ...)]`, no `signatures`
  - `a2a_tasks.send_message(auth: AuthContext, org: str, agent_id: str, message: dict, *, now: int | None = None) -> dict` (returns the A2A `Task` as a dict): requires `URI in message.get("extensions", [])` and `message["metadata"][URI]` (an `AuthorityRequest` with `operation.resource == f"agent:{agent_id}"`) else raises `A2AError(-32008)`; mints via Task 1 with `chain=[{"agent": None, "session": None, "person": auth.user_id, "at": now}]`, `verified_by="clerk-session"`; claims the id (replay → `A2AError(-32602, "replay")`); decides via Task 2 (rules from `a2a_repo.get_rules` or `default_rules(org, [auth.user_id] if auth.is_org_admin else [])`); `allow` → `contextId = await hosted_mcp.person_dm(auth, agent_id, create_key="a2a")`, posts the text parts joined by newlines with `create_human_message(..., via="lightbulb_mcp", author_override={"kind": "system", "id": f"a2a:{auth.user_id}", "displayName": f"A2A via {auth.user_id}"})`, stores the task `working` with `dispatch_message_id` and `room_id`; `deny` → task `rejected`; `hold` → Task 5; `reject/self` → `rejected`. Appends a record (spec §6 shape, signed with `sign_record`) on every state.
  - `a2a_tasks.get_task(org, task_id, *, now) -> dict` (A2A Task dict; computes `canceled`/`failed` on read per spec §5 step 6 and §6)
  - `a2a_tasks.on_agent_reply(owner_id: str, *, room_id: str, agent_id: str, message_id: str, text: str) -> None`: oldest `working` task in that room for that agent → `completed` with `artifact={"artifactId": message_id, "parts": [{"text": text}]}` and a record (`# ponytail: one open task per DM room completes in order`)
  - `a2a_tasks.to_a2a_task(row: dict) -> dict` (state map: `working→TASK_STATE_WORKING`, `held→TASK_STATE_AUTH_REQUIRED`, `completed→TASK_STATE_COMPLETED`, `rejected→TASK_STATE_REJECTED`, `canceled→TASK_STATE_CANCELED`, `failed→TASK_STATE_FAILED`; `Task.metadata[URI] = {"state": row["state"], "record": <latest record jws>, "rule": ...}`)
  - `class A2AError(Exception)`: `code: int`, `message: str`
  - Router `routers/a2a.py`: `router = APIRouter(tags=["a2a"])`; `GET /a2a/keys.json` → `{"keys": [network_proof.public_key()]}`; `GET /a2a/{org}/{agent_id}/agent-card.json` (no auth; `_public_ingress` bucket); `POST /a2a/{org}/{agent_id}` (`Depends(get_current_user)`, `ctx.org_id == org` else 403) parsing a JSON-RPC 2.0 envelope: `method == "SendMessage"` → `send_message(ctx, org, agent_id, params["message"])`; `"GetTask"` → `get_task(org, params["id"])`; `"CancelTask"` → error `-32004`; else `-32601`; errors as `{"jsonrpc":"2.0","id":..., "error": {"code", "message"}}`, HTTP 200
  - Agent identity for Friday: `agent_id` is the workspace agent's id as `messaging` knows it; name from the existing agent row. The builder locates the lookup (search `agent_id` in `core/services/messaging.py` room member creation) and names it in the report.

- [ ] **Step 1: Write the failing router tests**

```python
# tests/unit/routers/test_a2a.py  (uses conftest `client`, `mock_org_admin_context`, moto `repos`; patch hosted_mcp.person_dm and messaging.create_human_message with AsyncMock)
URI = "https://thelightbulb.company/a2a/ext/authority/v1"
def rpc(method, params, id=1): return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
def msg(auth_req, extensions=(URI,), key=URI):
    return {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hello"}], "extensions": list(extensions), "metadata": {key: auth_req}}
AUTH = {"operation": {"skill": "message", "resource": "agent:friday", "operands": {"text": "hello"}}, "intent": "say hello"}

def test_card_lists_required_extension(client):
    r = client.get("/a2a/org_test_456/friday/agent-card.json"); c = r.json()
    assert r.status_code == 200 and c["capabilities"]["extensions"][0] == {"uri": URI, "required": True, "description": c["capabilities"]["extensions"][0]["description"]}
    assert c["supportedInterfaces"][0]["protocolVersion"] == "1.0" and "signatures" not in c

def test_send_without_extension_is_32008(client_org_admin):
    r = client_org_admin.post("/a2a/org_test_456/friday", json=rpc("SendMessage", {"message": msg(AUTH, extensions=())}))
    assert r.json()["error"]["code"] == -32008

def test_send_with_wrong_metadata_key_is_32008(client_org_admin):
    r = client_org_admin.post("/a2a/org_test_456/friday", json=rpc("SendMessage", {"message": msg(AUTH, key="https://other/ext")}))
    assert r.json()["error"]["code"] == -32008

def test_allowed_send_dispatches_and_is_working_then_completes_on_reply(client_org_admin, person_dm, create_human_message):
    r = client_org_admin.post("/a2a/org_test_456/friday", json=rpc("SendMessage", {"message": msg(AUTH)}))
    task = r.json()["result"]; assert task["status"]["state"] == "TASK_STATE_WORKING"
    create_human_message.assert_awaited_once()
    import asyncio; from core.services import a2a_tasks
    asyncio.run(a2a_tasks.on_agent_reply("org_test_456", room_id=person_dm.return_value, agent_id="friday", message_id="am1", text="hi back"))
    t = client_org_admin.post("/a2a/org_test_456/friday", json=rpc("GetTask", {"id": task["id"]})).json()["result"]
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and t["artifacts"][0]["parts"][0]["text"] == "hi back"
    assert URI in t["metadata"] and t["metadata"][URI]["rule"] == "r1"

def test_two_working_tasks_complete_oldest_first(client_org_admin, person_dm, create_human_message):
    ...  # send twice, one reply, GetTask: first completed, second still working

def test_wrong_org_is_403_and_cancel_is_32004_and_unknown_method_is_32601(client_org_admin):
    assert client_org_admin.post("/a2a/org_other/friday", json=rpc("GetTask", {"id": "x"})).status_code == 403
    assert client_org_admin.post("/a2a/org_test_456/friday", json=rpc("CancelTask", {"id": "x"})).json()["error"]["code"] == -32004
    assert client_org_admin.post("/a2a/org_test_456/friday", json=rpc("Nope", {})).json()["error"]["code"] == -32601

def test_record_endpoint_returns_signed_records_oldest_first(client_org_admin, ...):
    ...  # after an allowed send: GET /a2a/org_test_456/friday/tasks/{id}/record → list of {record, jws}; verify the jws with a2a_authority (typ lightbulb-a2a-record) and record["rule"] == "r1"
```

`client_org_admin` is a sync `TestClient` variant of `async_client_org_admin` (`tests/conftest.py:258`); add it to the test module if conftest lacks it.

- [ ] **Step 2: Write the failing conformance test**

```python
# tests/integration/test_a2a_conformance.py  (marked integration; uses the official a2a-sdk client over httpx ASGITransport with the org-admin auth override)
async def test_official_client_fetches_card_and_sends_message(app_with_admin_override, repos, person_dm, create_human_message):
    from a2a.client import ...   # the SDK's client; the builder names the exact classes in the report
    card = await <client>.get_agent_card(url=".../a2a/org_test_456/friday/agent-card.json")
    assert card.capabilities.extensions[0].required is True
    task = await <client>.send_message(<Message with extensions=[URI], metadata={URI: AUTH}>)   # with the A2A-Extensions header set to URI
    assert task.status.state.name.endswith("WORKING")
```

- [ ] **Step 3: Run both files to verify they fail** — expected `ModuleNotFoundError: routers.a2a` / 404.
- [ ] **Step 4: Implement `a2a_tasks.py`, `routers/a2a.py`, the `main.py` include and the `post_agent_message` hook.**
- [ ] **Step 5: Run tests to verify they pass** — `uv run pytest tests/unit/routers/test_a2a.py tests/integration/test_a2a_conformance.py -v`, expected PASS (8 tests).
- [ ] **Step 6: Commit** — `git commit -m "feat(a2a): agent card, keys, SendMessage/GetTask, dispatch to a hosted agent, reply linking, records"`

---

### Task 5: Hold, holder notification, decide, self-approval, exception, expiry, policy change

**Files:**
- Modify: `apps/backend/core/services/a2a_tasks.py`
- Modify: `apps/backend/routers/a2a.py` (add `POST /a2a/{org}/{agent_id}/tasks/{task_id}/decide`, `GET /a2a/{org}/rules`, `PUT /a2a/{org}/rules`)
- Test: `apps/backend/tests/unit/routers/test_a2a_hold.py`

**Interfaces:**
- Consumes: `messaging.create_agent_message(owner_id, AgentMessageRequest(event_id, room_id, agent_id, text, reply_to_message_id=None, thread_root_message_id=None))` (`messaging.py:4827`, model at `:653`); `hosted_mcp.person_dm`; `require_org_admin` (`core/auth.py:125`).
- Produces:
  - In `send_message`: `hold` → task `held` with `holders`, `heldAt=now`; notify each holder: `room = await hosted_mcp.person_dm(AuthContext(user_id=holder, org_id=org), agent_id, create_key="a2a")` then `create_agent_message(org, AgentMessageRequest(event_id=f"a2a:{task_id}:hold", room_id=room, agent_id=agent_id, text=<"I need your OK to {skill} on {resource} for {requester}: {intent[:200]}. Answer at {base_url}/a2a/{org}/{agent_id}/tasks/{task_id}/decide">))`
  - `a2a_tasks.decide(auth: AuthContext, org, agent_id, task_id, *, verdict: Literal["allow","deny"], reason: str, now) -> dict`: task must be `held` and not expired on read (else `A2AError(409, "expired")`); `auth.user_id in holders` else `403`; `auth.user_id == requester_person` → `403` rule `self`; `allow` → `put_exception(...)`, then the same dispatch as an allowed send, record `allow_by_holder` with `answered_by`; `deny` → `rejected`, record `deny_by_holder`
  - `a2a_tasks.get_task`: a `held` task with `now - heldAt >= 86400` reads as `canceled` with reason `expired` (and writes that state once, with a record)
  - `a2a_tasks.put_rules(auth, org, rules: Rules, *, now)`: admin only; if `revision` changed, every `held` task → `canceled`, reason `policy_changed`, with a record
  - `GET /a2a/{org}/rules` (admin) → the document or `default_rules`; `PUT` (admin) → `put_rules`

- [ ] **Step 1: Write the failing tests**

```python
def test_ask_session_is_held_and_holder_is_notified(...):           # default rules; resource session:abc with session person user_owner (patch the session-person lookup); state AUTH_REQUIRED; create_agent_message awaited once with text containing "/decide"; metadata[URI]["state"] == "held"
def test_requester_cannot_decide_own_task_and_non_holder_is_403(...):  # requester → 403 with "self"; a third user → 403
def test_holder_allow_dispatches_writes_exception_and_record(...):   # holder → allow; task WORKING; records contain allow_by_holder with answered_by user_owner; get_exception returns a row
def test_second_identical_request_is_allowed_by_exception(...):      # same operands → allow, rule "exception:<id>"; different operands → held again
def test_expired_hold_reads_as_canceled_and_rejects_decide(...):     # now + 86401: GetTask CANCELED; decide → 409 expired; no allow_by_holder record
def test_rules_revision_change_cancels_held_tasks(...):              # PUT rules with a new revision as admin; held task → CANCELED reason policy_changed; member PUT → 403
```

- [ ] **Step 2: Run to verify they fail** — expected failures on the missing `decide`/routes.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify they pass** — expected PASS (6 tests).
- [ ] **Step 5: Commit** — `git commit -m "feat(a2a): held tasks, holder decision, exceptions, expiry, rules routes"`

---

### Task 6: Dispatch to a laptop session through the origin person's computer link

**Files:**
- Modify: `apps/backend/core/services/a2a_tasks.py`
- Test: `apps/backend/tests/unit/services/test_a2a_session_dispatch.py`

**Interfaces:**
- Consumes: `hosted_mcp._through_member(auth: AuthContext, op: str, *, sender: tuple[str, str] | None, **fields)` (`hosted_mcp.py:651`; the computer link path `network_route.app_call` at `network_route.py:142`, which raises when `best_link(user_id)` is `None`); `hosted_mcp._network_request_id`.
- Produces:
  - `a2a_tasks.session_header(*, requester_display: str, for_whom: str, intent: str, record_url: str) -> str` → ≤ 512 bytes, format `"[A2A] {requester_display} for {for_whom}: {intent[:200]} — record: {record_url}\n"`
  - In the allowed path when `resource` starts with `session:`: `await hosted_mcp._through_member(origin_ctx, "send", sender=(agent_id, agent_name), to=session_id, text=header + text, request_id=task_id)` where `origin_ctx = AuthContext(user_id=chain[0].person, org_id=org)`; on the link error → task `failed`, reason `undeliverable`, with a record; on success → `working` with `dispatch_request_id`
  - `# ponytail: the send rides the origin person's computer link; the cloud member bridge replaces this in phase 2`

- [ ] **Step 1: Write the failing tests**

```python
def test_header_is_capped_at_512_bytes_and_200_intent_chars(): ...
def test_session_dispatch_uses_origin_person_link(...):   # patch hosted_mcp._through_member AsyncMock; assert called with auth.user_id == origin person, to == "abc", text startswith "[A2A]", request_id == task id; task working
def test_no_link_fails_undeliverable_with_record(...):    # _through_member raises RuntimeError("no_link"); task FAILED; record reason undeliverable
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run to verify they pass** (3 tests). — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): laptop session dispatch through the origin person's computer link"`

---

### Task 7: Friday's `ask_session` tool and the internal route

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb/__init__.py` (add `ASK_SESSION_SCHEMA`, `_ask_session`, and `ctx.register_tool("ask_session", "lightbulb", ASK_SESSION_SCHEMA, _ask_session, description=..., emoji="🔐")` next to `:922`)
- Modify: `apps/infra/hermes/plugins/lightbulb/plugin.yaml` (`provides_tools` add `ask_session`)
- Modify: `apps/backend/routers/a2a.py` (add `POST /api/v1/internal/a2a/{owner_id}/ask-session`, guarded by `core.services.container_auth._require_hook_token(owner_id, request)` (`container_auth.py:28`))
- Modify: `apps/backend/core/services/a2a_tasks.py` (add `ask_session`)
- Test: `apps/infra/hermes/plugins/lightbulb/tests/test_ask_session.py` (next to the plugin's existing tests; if none exist, `apps/infra/hermes/tests/test_lightbulb_ask_session.py`), `apps/backend/tests/unit/routers/test_a2a_internal.py`

**Interfaces:**
- Consumes: `_post(url, token, body)` (`__init__.py:54`), `turn_context.read(session_id)` (keys `runId, agentId, ownerId, apiUrl, hookToken, roomId`); the hook-token test pattern in `tests/unit/routers/test_messaging_run_events.py`; the message author lookup for the origin message (`messaging_repo` get by room and message id; the builder names it).
- Produces:
  - Tool schema: `{"name": "ask_session", "description": "Ask one of the workspace's laptop sessions to do something, under the person who asked you. Held for that session's person when no rule allows it.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}, "text": {"type": "string"}, "intent": {"type": "string"}, "origin_message_id": {"type": "string"}}, "required": ["session_id", "text", "intent"]}}`
  - `_ask_session(args: dict, *, session_id: str = "", **_kw) -> str`: posts `{"sessionId", "text", "intent", "originMessageId": args.get("origin_message_id") or ctx.get("threadRootMessageId"), "agentId": ctx["agentId"], "runId": ctx["runId"]}` to `f"{ctx['apiUrl']}/api/v1/internal/a2a/{ctx['ownerId']}/ask-session"` with `ctx["hookToken"]`; returns `json.dumps` of the response (`taskId`, `state`, `holders`)
  - Route body model `AskSessionRequest(ContractModel)`: `session_id: str`, `text: str`, `intent: str`, `origin_message_id: str | None`, `agent_id: str`, `run_id: str | None`
  - `a2a_tasks.ask_session(owner_id, body, *, now) -> dict`: resolves `requester_person` from the origin message's author (a human author id) else the first admin holder of `agent:{agent_id}` with `verified_by="agent-runtime"`; builds `AuthorityRequest(operation=Operation(skill="ask_session", resource=f"session:{session_id}", operands={"text": text}), intent=intent)`; mints with `chain=[{person: requester}, {agent: agent_id, person: requester}]`; decides with `session_person` from the session row (`local_sessions_repo.get_session(owner_id, session_id)["humanUserId"]`, `local_sessions_repo.py:37`); then the Task 5 hold path or the Task 6 dispatch; returns `{"taskId", "state", "holders"}`

- [ ] **Step 1: Write the failing tests**

```python
# plugin test: patch _post and turn_context.read; call _ask_session({"session_id": "abc", "text": "status?", "intent": "founder asked"}, session_id="s1"); assert the URL ends with /api/v1/internal/a2a/OWNER/ask-session, the bearer is the hook token, and the body has sessionId/agentId/intent
# route tests: (1) wrong hook token → 401; (2) good token, origin message by user_x, no rule → response state "held", holders ["user_owner"]; (3) no origin message → requester is the admin holder, verified_by "agent-runtime" in the record
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run to verify they pass** (4 tests). — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): ask_session tool for hosted agents and the internal route"`

---

### Task 8: Replay script and the published extension page

**Files:**
- Create: `apps/backend/scripts/a2a_replay/__main__.py`, `apps/backend/scripts/a2a_replay/cases.json`
- Create: `docs/a2a-authority-extension.md` (the public spec text of the extension: URI, object fields, signing, verification order, errors, the rule document, the record; copied from spec §4-§6 with the phase-1 cuts applied)
- Test: `apps/backend/tests/unit/scripts/test_a2a_replay.py`

**Interfaces:**
- Consumes: `a2a_rules.decide`, `a2a_authority.operands_hash`.
- Produces: `cases.json` = a list of `{"name", "requester_person", "requester_org", "chain_agents", "resource", "skill", "session_person", "expected": "allow|deny|hold|reject"}` with at least these 12 cases: the Friday/Aden session request (hold); the same from the session's own person (reject self); an outside-org requester (deny); an in-org message to Friday (allow); the ServiceNow second-order case as `agent:x` chain to a protected resource with no rule (hold); the GitHub MCP public-issue case (deny by an explicit deny rule on `room:public`); the EchoLeak-style exfil to an external resource (deny outside org); a Salesloft-style token reuse with a mismatched org (deny); an operand-substitution pair (same intent, different operands → hold twice, one exception does not cover the other); a policy-revision case (held then canceled); a holder-allow case (allow via exception); and an autonomous Friday turn (requester admin, hold). `__main__.py` prints a table `name | expected | got | ok` and exits 1 on any mismatch. Invocation: `uv run python -m scripts.a2a_replay` from `apps/backend`. No model call; `# ponytail: deterministic cases; add LLM-generated variants on the Codex subscription when the compiled-rules increment lands`.

- [ ] **Step 1: Write the failing test** — `test_replay_cases_all_match()` runs the module's `run(cases) -> list[dict]` and asserts every row `ok` and that there are ≥ 12 rows.
- [ ] **Step 2: Run to verify it fails.** — [ ] **Step 3: Write `cases.json`, `__main__.py` and `docs/a2a-authority-extension.md`.** — [ ] **Step 4: Run to verify it passes.** — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): replay cases and the published extension page"`

---

## Not in this plan (phase 1b and 2, per spec §2a and §9)
Third-party OAuth clients; streaming; cancel; card signatures; `when` clauses; re-deciding on revision change; an in-app Approve/Deny button; chain re-signing and narrowing; P2P frames as A2A messages; daemon-side cards and holds; Workers rules.
