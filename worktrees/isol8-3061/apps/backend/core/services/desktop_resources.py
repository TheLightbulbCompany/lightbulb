"""Paired-Mac files and MCP. Remote descriptors are data, never tool registrations.

Files require a current native folder grant, then the pinned CLI's canonical inode
preflight. No request may provide policy, approval, symlink mode or inode bindings.
"""

import base64
import hashlib
import io
import posixpath
import re
import tarfile
import zlib

from core.services.desktop_execution import DesktopError, _session
from core.services.node_broker import CommandNotAllowed

FILE_COMMANDS = {"file.fetch", "file.write", "dir.list", "dir.fetch"}
COMMANDS = FILE_COMMANDS | {"mcp_list", "mcp_call"}
POLICY_COMMAND = "file.access.policy.v1"
FILE_BYTES = 4 * 1024 * 1024
WRITE_BYTES = 512 * 1024
MAX_ENTRIES = 1000


def node_command(command):
    return "mcp.tools.call.v1" if command in {"mcp_list", "mcp_call"} else command


def public_commands(commands):
    commands = set(commands)
    return commands | ({"mcp_list", "mcp_call"} if "mcp.tools.call.v1" in commands else set())


def path_valid(path):
    return (
        isinstance(path, str)
        and 1 < len(path) <= 4096
        and path.startswith("/")
        and "\\" not in path
        and not any(ord(c) < 32 for c in path)
        and posixpath.normpath(path) == path
        and "//" not in path
    )


def decode_bytes(value, limit, sha):
    try:
        if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
            raise ValueError()
        data = base64.b64decode(value, validate=True)
        if len(data) > limit or base64.b64encode(data).decode() != value or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise DesktopError("file_integrity_invalid") from None


def validate(command, params):
    if not isinstance(params, dict):
        raise DesktopError("invalid_command_params")
    if command == "mcp_list":
        if params:
            raise DesktopError("invalid_command_params")
        return params
    if command == "mcp_call":
        if set(params) != {"server", "tool", "arguments", "catalogVersion"} or not isinstance(
            params["arguments"], dict
        ):
            raise DesktopError("invalid_command_params")
        if any(
            not isinstance(params[k], str)
            or not 1 <= len(params[k]) <= 256
            or params[k].strip() != params[k]
            or any(ord(c) < 32 for c in params[k])
            for k in ("server", "tool", "catalogVersion")
        ):
            raise DesktopError("invalid_command_params")
        return params
    allowed = {"path"} | (
        {"contentBase64", "sha256", "overwrite", "createParents"}
        if command == "file.write"
        else {"maxEntries", "pageToken"}
        if command == "dir.list"
        else {"maxBytes"}
    )
    if set(params) - allowed or not path_valid(params.get("path")):
        raise DesktopError("invalid_command_params")
    if command == "file.write":
        # Pinned bound overwrite truncates in place. Only exclusive new-file creation
        # has the atomic/no-overwrite contract required here.
        if params.get("overwrite", False) is not False or type(params.get("createParents", False)) is not bool:
            raise DesktopError("file_overwrite_unavailable", 422)
        decode_bytes(params.get("contentBase64"), WRITE_BYTES, params.get("sha256"))
        return {**params, "overwrite": False, "createParents": params.get("createParents", False)}
    key, maximum = ("maxEntries", MAX_ENTRIES) if command == "dir.list" else ("maxBytes", FILE_BYTES)
    if type(params.get(key, maximum)) is not int or not 1 <= params.get(key, maximum) <= maximum:
        raise DesktopError("invalid_command_params")
    if "pageToken" in params and (
        not isinstance(params["pageToken"], str) or not re.fullmatch(r"[0-9]{1,8}", params["pageToken"])
    ):
        raise DesktopError("invalid_command_params")
    return {**params, key: params.get(key, maximum)}


def inside(path, root):
    return path == root or path.startswith(root + "/")


def check_path(policy, path, command):
    if (
        not isinstance(policy, dict)
        or set(policy) - {"readRoots", "writeRoots", "denyRoots", "nativeDeviceId", "nativeGeneration"}
        or not {"readRoots", "writeRoots", "denyRoots"} <= set(policy)
        or any(
            not isinstance(roots, list) or len(roots) > 64 or any(not path_valid(root) for root in roots)
            for roots in (policy["readRoots"], policy["writeRoots"], policy["denyRoots"])
        )
    ):
        raise DesktopError("file_policy_unavailable", 403)
    roots = policy["writeRoots" if command == "file.write" else "readRoots"]
    if (
        not path_valid(path)
        or not any(inside(path, root) for root in roots)
        or any(inside(path, root) for root in policy["denyRoots"])
    ):
        raise DesktopError("file_path_denied", 403)


def entry_path(root, entry):
    if not isinstance(entry, str):
        raise DesktopError("file_archive_invalid")
    entry = entry.removeprefix("./").rstrip("/")
    if not entry or entry == ".":
        return root
    if entry.startswith("/") or any(part in {"", ".", ".."} for part in entry.split("/")) or "\\" in entry:
        raise DesktopError("file_archive_invalid")
    return root + "/" + entry


def validate_archive(data, root, policy):
    # Bound decompression BEFORE tarfile parses PAX headers (which can allocate).
    total, seen = 0, set()
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        unpacked = decoder.decompress(data, FILE_BYTES + MAX_ENTRIES * 2048 + 1)
        if len(unpacked) > FILE_BYTES + MAX_ENTRIES * 2048 or not decoder.eof or decoder.unused_data.strip(b"\0"):
            raise DesktopError("file_archive_invalid")
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r|") as archive:
            for count, member in enumerate(archive):
                path = entry_path(root, member.name)
                check_path(policy, path, "dir.fetch")
                total += member.size
                if (
                    count >= MAX_ENTRIES
                    or total > FILE_BYTES
                    or member.size < 0
                    or not (member.isfile() or member.isdir())
                    or member.sparse
                    or path in seen
                ):
                    raise DesktopError("file_archive_invalid")
                seen.add(path)
            # Tar readers can resume past zero EOF blocks (ignore_zeros / tar -i).
            # Only padding may follow the last validated member's padded boundary.
            if unpacked[archive.offset :].strip(b"\0"):
                raise DesktopError("file_archive_invalid")
    except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error):
        raise DesktopError("file_archive_invalid") from None


async def _current(store, row, target):
    await store.authorize(row["auth"])
    current = await store.resolve(row["auth"], row["request"])
    if row["state"] in {"cancel_requested", "unknown"}:
        raise DesktopError("cancelled_before_dispatch", 409)
    if current != target or not store.enabled(row["auth"]["ownerId"]):
        raise DesktopError("desktop_generation_lost", 409)
    if store.clock() > row["deadline"]:
        raise DesktopError("file_approval_expired", 403)


async def execute(store, row, target):
    request, params = row["request"], row["request"]["params"]
    command = request["command"]
    await _current(store, row, target)
    if command in {"mcp_list", "mcp_call"}:
        if "mcp.tools.call.v1" not in target["commands"]:
            raise DesktopError("mcp_not_granted", 403)
        session = _session(target)
        if command == "mcp_list":
            return {"ok": True, "payload": session.mcp_catalog()}
        try:
            session.mcp_call(params["server"], params["tool"], params["catalogVersion"])
        except CommandNotAllowed:
            raise DesktopError("mcp_catalog_stale", 409) from None
        row.update(state="running", dispatched=True)
        result = await store._phase(
            row,
            target,
            request
            | {"command": "mcp.tools.call.v1", "params": {k: v for k, v in params.items() if k != "catalogVersion"}},
            row["invokeId"],
        )
        # Catalog removal during a call makes its output unavailable, not replayable.
        try:
            session.mcp_call(params["server"], params["tool"], params["catalogVersion"])
        except CommandNotAllowed:
            raise DesktopError("mcp_catalog_stale", 409) from None
        return result
    if store.file_policy is None:
        raise DesktopError("file_policy_unavailable", 403)
    policy = await store.file_policy(row["auth"], target, command)
    check_path(policy, params["path"], command)
    await _current(store, row, target)
    expires = min(row["deadline"], store.clock() + 30)
    forwarded = {k: v for k, v in params.items() if k != "sha256"} | {"followSymlinks": False}
    if command == "file.write":
        forwarded["expectedSha256"] = params["sha256"]
    prepared = await store._phase(
        row,
        target,
        request | {"params": forwarded | {"preflightOnly": True}, "_fileAuthorized": True},
        row["invokeId"] + "-preflight",
    )
    payload = prepared.get("payload")
    if not prepared.get("ok") or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise DesktopError("file_preflight_denied", 403)
    canonical, binding = payload.get("path"), payload.get("binding")
    check_path(policy, canonical, command)
    keys = (
        {"kind", "anchorPath", "anchorDevice", "anchorInode"}
        if command == "file.write"
        else {"kind", "device", "inode"}
    )
    if (
        not isinstance(binding, dict)
        or set(binding) != keys
        or binding.get("kind") != ("write" if command == "file.write" else "existing")
        or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 for v in binding.values())
    ):
        raise DesktopError("file_binding_invalid", 403)
    if command == "file.write":
        check_path(policy, binding["anchorPath"], command)
    entries = payload.get("entries") if command == "dir.fetch" else []
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise DesktopError("file_archive_invalid")
    for entry in entries:
        check_path(policy, entry_path(canonical, entry), command)
    current_policy = await store.file_policy(row["auth"], target, command)
    if current_policy != policy or store.clock() > expires:
        raise DesktopError("file_approval_expired", 403)
    await _current(store, row, target)
    if store.clock() > expires:
        raise DesktopError("file_approval_expired", 403)
    row.update(state="running", dispatched=True)
    result = await store._phase(
        row,
        target,
        request
        | {
            "params": forwarded | {"expectedCanonicalPath": canonical, "expectedBinding": binding},
            "_fileAuthorized": True,
        },
        row["invokeId"],
    )
    payload = result.get("payload")
    if not result.get("ok") or not isinstance(payload, dict) or payload.get("ok") is not True:
        return {"ok": False, "error": {"code": "file_node_failed"}}
    if payload.get("path") != canonical:
        raise DesktopError("file_canonical_changed", 409)
    if command in {"file.fetch", "dir.fetch"}:
        data = decode_bytes(
            payload.get("tarBase64" if command == "dir.fetch" else "base64"), params["maxBytes"], payload.get("sha256")
        )
        if command == "dir.fetch":
            validate_archive(data, canonical, policy)
        if payload.get("tarBytes" if command == "dir.fetch" else "size") != len(data):
            raise DesktopError("file_integrity_invalid")
        row["_artifacts"] = [data]
        if store.trim_bodies(row["socket"]) > 8 * 1024 * 1024:
            raise DesktopError("result_too_large", 413)
        name = "directory.tar.gz" if command == "dir.fetch" else posixpath.basename(canonical)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}", name) or ".." in name or name.endswith((".", " ")):
            name = "download.bin"
        return {
            "ok": True,
            "payload": {
                "artifacts": [
                    {
                        "id": 0,
                        "name": name,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "mimeType": "application/gzip" if command == "dir.fetch" else "application/octet-stream",
                    }
                ]
            },
        }
    if command == "file.write" and (
        payload.get("sha256") != params["sha256"] or payload.get("overwritten") is not False
    ):
        raise DesktopError("file_integrity_invalid")
    if command == "dir.list":
        if not isinstance(payload.get("entries"), list) or len(payload["entries"]) > params["maxEntries"]:
            raise DesktopError("file_listing_invalid")
        for entry in payload["entries"]:
            if not isinstance(entry, dict):
                raise DesktopError("file_listing_invalid")
            check_path(policy, entry.get("path"), command)
    return result
