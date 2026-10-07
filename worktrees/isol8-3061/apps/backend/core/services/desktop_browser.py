"""Typed selected-Mac browser protocol for the pinned OpenClaw 2026.8.1 engine.

Model input never supplies an HTTP route, local pathname or CDP endpoint.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from core.services.desktop_execution import DesktopError, MAX_BODY_BYTES

# Required/optional public arguments. Browser identity lives outside these arguments.
ARGUMENTS = {
    "profiles": (set(), set()),
    "status": (set(), set()),
    "tabs": (set(), set()),
    "open": ({"url"}, set()),
    "focus": (set(), set()),
    "close": (set(), set()),
    "navigate": ({"url"}, set()),
    "snapshot": (set(), set()),
    "screenshot": (set(), {"ref"}),
    "pdf": (set(), set()),
    "click": ({"ref"}, set()),
    "type": ({"ref", "text"}, {"submit"}),
    "press": ({"key"}, set()),
    "hover": ({"ref"}, set()),
    "drag": ({"startRef", "endRef"}, set()),
    "select": ({"ref", "values"}, set()),
    "fill": ({"fields"}, set()),
    "wait": ({"text"}, set()),
    "upload": ({"ref", "files"}, set()),
    "download": ({"ref"}, set()),
}
PROFILE_ACTIONS = {"profiles", "status", "tabs", "open"}
REF_FIELDS = {"ref", "startRef", "endRef"}
MAX_UPLOAD_BYTES = 512 * 1024
MAX_UPLOAD_REQUEST = 768 * 1024
MAX_MEDIA_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


def decode_file(value, limit):
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise DesktopError("browser_file_too_large", 413)
    try:
        data = base64.b64decode(value, validate=True)
        if len(data) > limit or base64.b64encode(data).decode() != value:
            raise ValueError()
    except (ValueError, binascii.Error):
        raise DesktopError("invalid_browser_file") from None
    return data


def safe_name(name):
    return (
        isinstance(name, str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}", name))
        and ".." not in name
        and not name.endswith((".", " "))
    )


def validate(params):
    if not isinstance(params, dict) or set(params) - {"action", "profile", "target", "browserGeneration", "args"}:
        raise DesktopError("invalid_browser_params")
    action, profile, args = params.get("action"), params.get("profile"), params.get("args", {})
    if not isinstance(action, str) or action not in ARGUMENTS:
        raise DesktopError("unsupported_browser_action", 422)
    if not isinstance(profile, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile):
        raise DesktopError("invalid_browser_profile")
    required, optional = ARGUMENTS[action]
    if not isinstance(args, dict) or required - args.keys() or args.keys() - required - optional:
        raise DesktopError("invalid_browser_args")
    for key in ("target", "browserGeneration"):
        value = params.get(key)
        if action not in PROFILE_ACTIONS and value is None:
            raise DesktopError("browser_target_required")
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", value)):
            raise DesktopError("invalid_browser_target")
    if action in PROFILE_ACTIONS and ("target" in params or "browserGeneration" in params):
        raise DesktopError("invalid_browser_target")
    for key, value in args.items():
        if key in REF_FIELDS | {"key", "text", "url"} and (
            not isinstance(value, str) or not value or len(value) > 8192 or "\x00" in value
        ):
            raise DesktopError("invalid_browser_args")
        if key in REF_FIELDS and not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", value):
            raise DesktopError("invalid_browser_ref")
    if "url" in args:
        try:
            url = urlsplit(args["url"])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError()
        except ValueError:
            raise DesktopError("invalid_browser_url") from None
    if "submit" in args and type(args["submit"]) is not bool:
        raise DesktopError("invalid_browser_args")
    if "values" in args and (
        not isinstance(args["values"], list)
        or not 1 <= len(args["values"]) <= 16
        or any(not isinstance(v, str) or len(v) > 1024 for v in args["values"])
    ):
        raise DesktopError("invalid_browser_args")
    if "fields" in args:
        fields = args["fields"]
        if not isinstance(fields, list) or not 1 <= len(fields) <= 16:
            raise DesktopError("invalid_browser_args")
        for field in fields:
            if (
                not isinstance(field, dict)
                or set(field) != {"ref", "type", "value"}
                or not isinstance(field["ref"], str)
                or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", field["ref"])
                or not isinstance(field["type"], str)
                or field["type"] not in {"text", "checkbox", "radio"}
                or type(field["value"]) not in {str, bool}
                or len(str(field["value"])) > 4096
            ):
                raise DesktopError("invalid_browser_args")
    if action == "upload":
        files = args["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 4:
            raise DesktopError("invalid_browser_files")
        total = 0
        names = set()
        for file in files:
            if (
                not isinstance(file, dict)
                or set(file) != {"name", "contentBase64", "sha256"}
                or not safe_name(file["name"])
                or file["name"] in names
            ):
                raise DesktopError("invalid_browser_files")
            data = decode_file(file["contentBase64"], MAX_UPLOAD_BYTES - total)
            if hashlib.sha256(data).hexdigest() != file["sha256"]:
                raise DesktopError("browser_file_hash_mismatch")
            names.add(file["name"])
            total += len(data)
    return {**params, "args": args}


def proxy(action, profile, target, args, timeout):
    method, path, query, body = "POST", "/act", {}, {"targetId": target, **args}
    if action == "_profile":
        # Literal validation-only probe: pinned route rejects before page access.
        path, body = "/tabs/focus", {}
    elif action in {"status", "tabs", "snapshot"}:
        method, path, body = "GET", {"status": "/", "tabs": "/tabs", "snapshot": "/snapshot"}[action], None
        if action == "snapshot":
            query = {"targetId": target, "format": "ai", "maxChars": 32000, "limit": 500}
    elif action in {"open", "focus"}:
        path = "/tabs/" + action
        if action == "open":
            body = args
    elif action == "close":
        method, path, body, query = "DELETE", "/tabs/" + target, None, {"targetIdMode": "raw"}
    elif action in {"navigate", "pdf", "screenshot", "download"}:
        path = "/" + action
        if action == "screenshot":
            body |= {"type": "jpeg", "fullPage": False}
        elif action == "download":
            # Upstream requires a path under its managed download root. It never
            # comes from the model; a unique basename cannot overwrite another file.
            body["path"] = f"lightbulb-{uuid.uuid4().hex}.download"
    elif action == "upload":
        path, body = "/hooks/file-chooser", {"targetId": target, "ref": args["ref"]}
    else:
        body["kind"] = action
    request = {
        "method": method,
        "path": path,
        "profile": profile,
        "query": query,
        "body": body,
        "timeoutMs": timeout,
        "errorEnvelope": "browser-v1",
    }
    if action == "upload":
        request["upload"] = {
            "envelope": "browser-upload-v1",
            "files": [{"name": f["name"], "contentBase64": f["contentBase64"]} for f in args["files"]],
        }
    return request


def media_result(payload, row):
    files = payload.get("files", [])
    if not isinstance(files, list) or len(files) > 4:
        raise DesktopError("browser_file_too_large", 413)
    result = payload["result"]
    paths = {result.get("path"), result.get("imagePath")}
    downloads = [result.get("download", {}), *result.get("downloads", [])]
    paths.update(d.get("path") for d in downloads if isinstance(d, dict))
    metadata, data, total, seen = [], [], 0, set()
    for file in files:
        if not isinstance(file, dict) or set(file) - {"path", "base64", "mimeType"}:
            raise DesktopError("invalid_browser_file")
        path = file.get("path")
        if (
            not isinstance(path, str)
            or path not in paths
            or path in seen
            or not path.startswith("/")
            or ".." in PurePosixPath(path).parts
            or "\x00" in path
        ):
            raise DesktopError("invalid_browser_file")
        binary = decode_file(file.get("base64"), MAX_MEDIA_BYTES - total)
        mime = file.get("mimeType") or "application/octet-stream"
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime):
            raise DesktopError("invalid_browser_file")
        download = next((item for item in downloads if isinstance(item, dict) and item.get("path") == path), {})
        name = download.get("suggestedFilename") or PurePosixPath(path).name
        if not safe_name(name):
            name = "browser-artifact"
        metadata.append(
            {
                "id": len(data),
                "name": name,
                "mimeType": mime,
                "bytes": len(binary),
                "sha256": hashlib.sha256(binary).hexdigest(),
            }
        )
        data.append(binary)
        total += len(binary)
        seen.add(path)
    if any(isinstance(path, str) and path not in seen for path in paths):
        raise DesktopError("browser_file_missing", 409)

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k not in {"path", "imagePath", "cdpUrl", "wsUrl", "userDataDir", "base64", "contentBase64"}
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    sanitized = clean(result)
    if len(json.dumps(sanitized).encode()) > 512 * 1024:
        raise DesktopError("browser_result_too_large", 413)
    if metadata:
        row["_artifacts"] = data
        sanitized["artifacts"] = metadata
    return sanitized


def unpack(response, profile, *, metadata=False):
    if not isinstance(response, dict) or not response.get("ok"):
        raise DesktopError("browser_node_failed", 409)
    payload = response.get("payload")
    if not isinstance(payload, dict):
        raise DesktopError("invalid_browser_result", 409)
    route = payload.get("route", {})
    if (
        route.get("status") != "resolved"
        or route.get("profile") != profile
        or route.get("driver") not in {"openclaw", "extension", "existing-session"}
    ):
        raise DesktopError("browser_profile_mismatch", 409)
    if metadata:
        if set(payload) != {"route", "error"} or payload["error"] != {
            "status": 400,
            "body": {"error": "targetId is required"},
        }:
            raise DesktopError("browser_protocol_mismatch", 409)
        return {"route": route, "result": {}}
    if "error" in payload:
        error = payload["error"]
        if not isinstance(error, dict) or type(error.get("status")) is not int:
            raise DesktopError("invalid_browser_result", 409)
        raise DesktopError("browser_action_unsupported" if error["status"] == 501 else "browser_route_failed", 409)
    if not isinstance(payload.get("result"), dict):
        raise DesktopError("invalid_browser_result", 409)
    return payload


class BrowserScopes:
    """Bounded ephemeral snapshot leases. Native CDP IDs are never URL-rescued.

    The generation is a lease over an observed native target, not a guessed Chrome
    process counter. The engine still enforces document/ref and extension leases.
    Chrome MCP cannot prove atomic numeric-page ownership and stays unavailable.
    """

    def __init__(self):
        self.leases = {}
        self.locks = {}
        self.lock_users = {}

    def prune(self, now):
        self.leases = {k: v for k, v in self.leases.items() if v["expires"] > now}
        live = {v["key"] for v in self.leases.values()}
        self.locks = {k: v for k, v in self.locks.items() if self.lock_users.get(k, 0) or k in live}

    @asynccontextmanager
    async def locked(self, key):
        if key not in self.locks and len(self.locks) >= 128:
            raise DesktopError("browser_scope_capacity", 429)
        lock = self.locks.setdefault(key, asyncio.Lock())
        self.lock_users[key] = self.lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self.lock_users[key] -= 1
            if not self.lock_users[key]:
                self.lock_users.pop(key)

    def issue(self, row, key, target, driver, now, refs=None):
        if driver == "existing-session" or not isinstance(target, str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", target):
            return {"unavailableReason": "browser_ownership_unverifiable"}
        if refs is None:
            for token, lease in self.leases.items():
                if (
                    lease["key"] == key
                    and lease["auth"] == row["auth"]
                    and lease["target"] == target
                    and lease["driver"] == driver
                    and lease["expires"] > now
                ):
                    return {"browserGeneration": token}
        if len(self.leases) >= 128:
            raise DesktopError("browser_scope_capacity", 429)
        token = uuid.uuid4().hex
        self.leases[token] = {
            "key": key,
            "auth": row["auth"],
            "target": target,
            "driver": driver,
            "refs": set(refs or {}),
            "expires": now + 300,
        }
        return {"browserGeneration": token}

    async def execute(self, store, row, target):
        p = row["request"]["params"]
        action, profile, args = p["action"], p["profile"], p["args"]
        if action == "profiles":
            # Upstream /profiles starts ephemeral MCP for EVERY profile, including
            # its injected `user` default, before applying allowProfiles. Never call it.
            return {
                "ok": True,
                "payload": {
                    "profiles": [
                        {"name": name, "driver": driver, "readiness": "unknown", "requiresLocalSetup": True}
                        for name, driver in (("lightbulb", "openclaw"), ("chrome", "extension"))
                    ],
                    "note": "App profile modes; select a locally configured profile and request status.",
                },
            }
        key = (*row["socket"], profile)
        self.prune(store.clock())
        async with self.locked(key):
            phase = 0
            expected_driver = None

            async def call(which, tab=None, arguments=None, *, effect=False):
                nonlocal phase
                await store.authorize(row["auth"])
                command = "browser.proxy.upload.v1" if which == "upload" else "browser.proxy"
                current = await store.resolve(row["auth"], row["request"] | {"command": command})
                if current["generation"] != target["generation"] or current["taskAddress"] != target["taskAddress"]:
                    raise DesktopError("desktop_generation_lost", 409)
                if not store.enabled(row["auth"]["ownerId"]):
                    raise DesktopError("desktop_disabled", 503)
                if row["state"] in {"cancel_requested", "unknown"}:
                    raise DesktopError("cancelled_before_dispatch", 409)
                request = row["request"] | {
                    "command": command,
                    "params": proxy(which, profile, tab, arguments or {}, row["request"]["executionDeadlineMs"]),
                }
                phase += 1
                if effect:
                    row.update(state="running", dispatched=True)
                response = await store._phase(row, current, request, row["invokeId"] + f"-browser-{phase}")
                payload = unpack(response, profile, metadata=which == "_profile")
                if expected_driver is not None and payload["route"]["driver"] != expected_driver:
                    raise DesktopError("browser_driver_changed", 409)
                return payload

            driver = (await call("_profile"))["route"]["driver"]
            expected_driver = driver
            if driver not in {"openclaw", "extension", "existing-session"}:
                raise DesktopError("browser_driver_unsupported", 422)
            if driver == "existing-session":
                raise DesktopError("browser_ownership_unverifiable", 422)
            if action == "status":
                payload = (await call("status"))["result"]
                return {
                    "ok": True,
                    "payload": {
                        "profile": profile,
                        "driver": driver,
                        **{k: payload[k] for k in ("running", "cdpReady", "transport") if k in payload},
                    },
                }
            tabs = (await call("tabs"))["result"].get("tabs", [])
            if not isinstance(tabs, list) or len(tabs) > 256 or any(not isinstance(v, dict) for v in tabs):
                raise DesktopError("invalid_browser_result", 409)
            if action == "tabs":
                result = []
                for tab in tabs:
                    identity = tab.get("targetId", "")
                    result.append(
                        {k: tab[k] for k in ("targetId", "title", "url", "type") if k in tab}
                        | self.issue(row, key, identity, driver, store.clock())
                    )
                return {"ok": True, "payload": {"profile": profile, "driver": driver, "tabs": result}}
            tab = p.get("target")
            if action != "open":
                lease = self.leases.get(p["browserGeneration"], {})
                if (
                    lease.get("key") != key
                    or lease.get("auth") != row["auth"]
                    or lease.get("target") != tab
                    or lease.get("driver") != driver
                    or lease.get("expires", 0) <= store.clock()
                    or not any(v.get("targetId") == tab for v in tabs)
                ):
                    raise DesktopError("browser_target_stale", 409)
                refs = [v for k, v in args.items() if k in REF_FIELDS]
                refs += [v["ref"] for v in args.get("fields", [])]
                if any(ref not in lease["refs"] for ref in refs):
                    raise DesktopError("browser_ref_stale", 409)
                # One snapshot owner at a time across runs/agents for this native tab.
                if action not in {"screenshot", "pdf", "wait", "focus"}:
                    self.leases = {k: v for k, v in self.leases.items() if v["key"] != key or v["target"] != tab}
            payload = await call(action, tab, args, effect=True)
            if action not in {"screenshot", "pdf", "download"} and payload.get("files"):
                raise DesktopError("unexpected_browser_media", 409)
            result = payload["result"]
            if action != "open" and result.get("targetId", tab) != tab:
                raise DesktopError("browser_target_changed", 409)
            result = media_result(payload, row)
            # Account for decoded bytes before the coordinator awaits completion
            # authorization. Active upload bodies share this same socket budget.
            if row.get("_artifacts") and store.trim_bodies(row["socket"]) > MAX_BODY_BYTES:
                row.pop("_artifacts", None)
                raise DesktopError("browser_media_capacity", 429)
            result |= {"profile": profile, "driver": driver}
            if action in {"snapshot", "open"}:
                result |= self.issue(row, key, result.get("targetId", tab), driver, store.clock(), result.get("refs"))
            return {"ok": True, "payload": result}
