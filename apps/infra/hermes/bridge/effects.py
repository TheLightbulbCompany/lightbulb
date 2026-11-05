"""The F1 checkpoint callback for a Worker's external, irreversible effects (wave2-Q Task 5; contracts-v2 §8).

Only sends, publishes, merges, purchases, provisioning and network sends ask the backend
(`POST /api/v1/internal/runtime/{owner}/effects`); in-VM shell and file work never does. The backend answers
`proceed | replay | refuse | needs_approval | denied`; `needs_approval` refuses the call and marks the run outcome
`waiting_approval`, so the turn ends and a person decides. A backend that cannot answer refuses the call (fail closed).

`classify` covers the shipped scripts and tools; shell egress that bypasses them (a raw `curl` to SES) is the accepted
residual (spec 05 R3). The runtime never reads flags: the backend asks for this by sending `effects: "required"`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from bridge import API, HOOK_TOKEN, OWNER

log = logging.getLogger("bridge")

EFFECTS_PATH = f"/api/v1/internal/runtime/{OWNER}/effects"
EXTERNAL_KINDS = ("mail.send", "text.send", "app.publish", "pr.merge", "purchase", "provision", "a2a.send")
MULTIPLE = "multiple"  # classify(): more than one external action in one command
SPLIT_THEM = "One external action per command; split them and run each on its own."
_WRAPPERS = {"env", "nohup", "time", "exec", "command", "builtin", "sudo", "timeout", "doas", "nice", "stdbuf",
             "ionice", "caffeinate"}
_VALUED_OPTS = {"-n", "-c", "-p", "-t", "-u", "-g", "-s", "--signal", "-k", "--kill-after", "-h", "-C", "-D", "-R", "-T",
                "-U", "-i", "-o", "-e"}  # a wrapper option whose value is the next token (nice -n 10, ionice -c 2 …)
_SEPARATOR = re.compile(r"^[;|&]+$")
_SHELL_C = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")  # -c, -lc, -ec, -xc …
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_DECISIONS = {"proceed", "replay", "refuse", "needs_approval", "denied"}
UNAVAILABLE = "Lightbulb couldn't check this action (effects service unavailable). Do not retry it this turn; say so."
_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_PR_URL = re.compile(r"^https?://github\.com/([^/]+/[^/]+)/pull/(\d+)")
_exit = os._exit  # dev-only fault hooks; tests patch it


@dataclass(frozen=True)
class EffectCall:
    kind: str
    operands: dict = field(hash=False, compare=True)
    executor_ok: bool  # the backend can land it from `operands` alone (Landing); else the Worker runs it


def operands_hash(call: EffectCall) -> str:
    canonical = json.dumps({"kind": call.kind, **call.operands}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# --- classifier ------------------------------------------------------------------------------------------


def _segments(command: str) -> list[tuple[list[str], str | None]]:
    """Each simple command's argv plus its own heredoc body (None when the body comes from elsewhere)."""
    out: list[tuple[list[str], str | None]] = []
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        line, body = lines[i], None
        i += 1
        if m := _HEREDOC_OPEN.search(line):
            line = line[: m.start()] + line[m.end() :]
            collected = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                collected.append(lines[i])
                i += 1
            i += 1  # the terminator
            body = "\n".join(collected)
        for argv, first in _commands(line):
            argv = _unwrap(argv)
            if not argv:
                continue
            if os.path.basename(argv[0]) in _SHELLS:
                flag = next((i for i, a in enumerate(argv[1:], 1) if _SHELL_C.match(a)), None)
                if flag is not None and flag + 1 < len(argv):  # `bash -lc "<script>"`: the script is what runs
                    out.extend(_segments(argv[flag + 1]))
                    continue
            out.append((argv, body if first else None))
    return out


def _commands(line: str) -> list[tuple[list[str], bool]]:
    """The simple commands of one line, quote-aware: `(argv, first_in_its_pipeline)` per `;`, `&&`, `||`, `|`."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";|&")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = line.split()
    out: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    first = True
    for t in tokens:
        if _SEPARATOR.match(t):  # ; && || | & |& ;; …
            if cur:
                out.append((cur, first))
            cur, first = [], "|" not in t
        else:
            cur.append(t)
    if cur:
        out.append((cur, first))
    return out


def _unwrap(argv: list[str]) -> list[str]:
    """Drop `env`, `VAR=x`, `timeout 30`, `nohup`, `sudo -u x`, `time`, `exec` wrappers in front of the real command."""
    i = 0
    while i < len(argv):
        a = argv[i]
        base = os.path.basename(a)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", a):
            i += 1
        elif base in ("timeout",):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in ("-s", "--signal", "-k", "--kill-after") else 1
            i += 1  # the duration
        elif base in _WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in _VALUED_OPTS else 1
        else:
            break
    return argv[i:]


def _flags(argv: list[str], *, valued: set[str], bare: set[str]) -> tuple[dict[str, str], set[str], list[str]]:
    values: dict[str, str] = {}
    seen: set[str] = set()
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in valued and i + 1 < len(argv):
            values[a], i = argv[i + 1], i + 2
        elif a in bare:
            seen.add(a)
            i += 1
        else:
            rest.append(a)
            i += 1
    return values, seen, rest


def _classify_shell(argv: list[str], body: str | None, agent_id: str | None) -> EffectCall | None:
    prog = os.path.basename(argv[0])
    if prog in ("lightbulb-mail.sh", "lightbulb-text.sh") and argv[1:2] == ["send"]:
        kind = "mail.send" if prog == "lightbulb-mail.sh" else "text.send"
        values, _, _ = _flags(argv[2:], valued={"--to", "--subject", "--reply", "--as"}, bare={"--owner"})
        operands: dict = {"agentId": values.get("--as") or agent_id}
        if "--to" in values:
            operands["to"] = values["--to"]
        if "--subject" in values:
            operands["subject"] = values["--subject"]
        if "--reply" in values:
            operands["replyKey"] = values["--reply"]
        if body is not None:
            operands["body"] = body
        return EffectCall(kind, operands, executor_ok=body is not None and bool(operands["agentId"]))
    if prog == "lightbulb" and argv[1:3] == ["app", "publish"]:
        values, _, _ = _flags(argv[3:], valued={"--app"}, bare={"--json"})
        app_id = values.get("--app")
        return EffectCall("app.publish", {"appId": app_id} if app_id else {}, executor_ok=bool(app_id))
    if prog == "gh" and argv[1:3] == ["pr", "merge"]:
        values, seen, rest = _flags(argv[3:], valued={"-R", "--repo"}, bare={"--squash", "--merge", "--rebase", "--auto", "--delete-branch", "-d"})
        operands = {}
        repo = values.get("-R") or values.get("--repo")
        if rest and (m := _PR_URL.match(rest[0])):
            repo, operands["number"] = m.group(1), int(m.group(2))
        elif rest and rest[0].lstrip("#").isdigit():
            operands["number"] = int(rest[0].lstrip("#"))
        if repo:
            operands["repo"] = repo
        operands["method"] = next((f.lstrip("-") for f in ("--squash", "--rebase") if f in seen), "merge")
        return EffectCall("pr.merge", operands, executor_ok="repo" in operands and "number" in operands)
    return None


def classify(tool: str, args: dict | None, *, agent_id: str | None = None) -> EffectCall | None:
    """The external effect one tool call makes, or None (local work: no network, `proceed`)."""
    args = args or {}
    if tool == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        found = [c for argv, body in _segments(command) if (c := _classify_shell(argv, body, agent_id))]
        if not found:
            return None
        if len(found) > 1:  # one approval can't cover two actions: refused locally, the backend is never asked
            return EffectCall(MULTIPLE, {"kinds": [c.kind for c in found]}, executor_ok=False)
        return found[0]
    if tool == "request_purchase":
        keep = {k: args[k] for k in ("merchant", "url", "amount", "why") if k in args}
        return EffectCall("purchase", keep, executor_ok=False)
    if tool == "lightbulb_network" and args.get("action") == "send":
        keep = {k: args[k] for k in ("to", "text", "reply_to") if k in args}
        return EffectCall("a2a.send", keep, executor_ok=False)
    return None


# --- backend ------------------------------------------------------------------------------------------------


def _post(path: str, body: dict, *, timeout_s: float = 8.0) -> tuple[int, dict]:
    """One POST with the hook token; (status, json). Raises on a network error. Under F1's 10 s callback timeout."""
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


_EXIT_RE = re.compile(r"\b(?:exit(?:ed with)?(?: code| status)?|return(?:ed|code))\s*[:=]?\s*(\d+)\b", re.IGNORECASE)


def _failed(result) -> bool:
    """A tool error object, or a plain-text terminal result that reports a non-zero exit."""
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            result = json.loads(result)
        except ValueError:
            return False
    if isinstance(result, dict):
        if result.get("error"):
            return True
        code = result.get("exit_code", result.get("exitCode", result.get("returncode")))
        return isinstance(code, int) and code != 0
    if isinstance(result, str):
        codes = [int(m.group(1)) for m in _EXIT_RE.finditer(result)]
        return bool(codes) and codes[-1] != 0
    return False


class EffectsCallback:
    """`before_tool(ref) -> Decision`, `tool_committed(ref, result, disposition)` for `agent.lb_checkpoint.install`.

    `outcome` becomes "waiting_approval" when a call needs a person's answer; the run wiring reports the turn with it.
    """

    def __init__(self, identity: dict, request: dict) -> None:
        self.task_id = identity["taskId"]
        self.attempt = int(identity["attempt"])
        self.run_id = identity["runId"]
        self.run_secret = identity.get("runSecret")
        self.agent_id = identity.get("agentId")
        self.faults = set(request.get("faults") or ())
        self.outcome: str | None = None
        self._open: dict[str, str] = {}  # tool_call_id → idem key of a proceed awaiting its commit

    def _decision(self):
        from agent.lb_checkpoint import Decision  # the fork image; absent means the run was refused earlier

        return Decision

    def before_tool(self, ref):
        call = classify(ref.name, ref.args, agent_id=self.agent_id)
        Decision = self._decision()
        if call is None:
            return Decision.proceed()
        if call.kind == MULTIPLE:
            return Decision.refuse(SPLIT_THEM)
        idem = f"{self.task_id}:{self.attempt}:{ref.tool_call_id}"
        body = {
            "taskId": self.task_id,
            "attempt": self.attempt,
            "runId": self.run_id,
            "runSecret": self.run_secret,
            "toolCallId": ref.tool_call_id,
            "kind": call.kind,
            "tool": ref.name,
            "operands": call.operands,
            "operandsHash": operands_hash(call),
            "executorOk": call.executor_ok,
            "idemKey": idem,
        }
        try:
            status, data = _post(EFFECTS_PATH, body)
        except Exception as exc:  # noqa: BLE001 -- fail closed, whatever went wrong
            log.warning("effects check failed run=%s: %s", self.run_id, type(exc).__name__)
            return Decision.refuse(UNAVAILABLE)
        decision = data.get("decision") if status == 200 and isinstance(data, dict) else None
        if decision not in _DECISIONS:
            log.warning("effects check unusable run=%s status=%s", self.run_id, status)
            return Decision.refuse(UNAVAILABLE)
        if decision == "proceed":
            self._open[ref.tool_call_id] = idem
            if "kill_after_effect_intent" in self.faults:
                _exit(70)  # the VM dies with the intent recorded and the tool not run
            return Decision.proceed()
        if decision == "replay":
            result = str(data.get("result") or "")
            if result.startswith("refused:"):  # it never happened: a replay would read as success
                return Decision.refuse(f"That exact action was refused earlier ({result.removeprefix('refused:')}). Do not retry it this turn.")
            return Decision.replay(result)
        if decision == "refuse":
            return Decision.refuse(str(data.get("message") or "Lightbulb refused this action."))
        qid = data.get("questionId") or "a question"
        if decision == "denied":
            return Decision.refuse(f"A person already said no to this exact action ({qid}). Do not retry it; do something else or say so.")
        self.outcome = "waiting_approval"
        return Decision.refuse(f"Waiting for a person's decision ({qid}). End your turn now; the answer arrives as input.")

    def tool_committed(self, ref, result, disposition) -> None:
        idem = self._open.pop(ref.tool_call_id, None)
        if idem is None:
            return
        if "kill_after_tool_before_done" in self.faults:
            _exit(70)
            return  # unreachable in the image (os._exit); tests patch _exit
        final = "done" if disposition == "done" or (disposition is None and not _failed(result)) else "unknown"
        body = {"disposition": final, "resultRef": None, "runSecret": self.run_secret, "runId": self.run_id}
        try:
            status, _ = _post(f"{EFFECTS_PATH}/{idem}/done", body)
            if status != 200:
                log.warning("effect done not recorded run=%s idem=%s status=%s", self.run_id, idem, status)
        except Exception as exc:  # noqa: BLE001 -- the intent row stays; the gate refuses a repeat until a person looks
            log.warning("effect done post failed run=%s idem=%s: %s", self.run_id, idem, type(exc).__name__)
