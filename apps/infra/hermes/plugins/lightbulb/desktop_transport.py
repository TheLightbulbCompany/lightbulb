"""Selected-Mac request plumbing (HTTP, receipts, uploads). Files land in this Hermes
profile, never Mac paths. Browsing on a Mac is use_browser (the bridge's browser_*
tools), not this module. No retry is performed after a possibly dispatched effect.
"""

import base64
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import urllib.request
import urllib.error
import uuid

from . import turn_context

UPLOAD_BYTES = 512 * 1024
MEDIA_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


def _name(name):
    return (
        isinstance(name, str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}", name))
        and ".." not in name
        and not name.endswith((".", " "))
    )


def upload_files(paths, root):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 4:
        raise ValueError("Upload needs one to four profile files")
    root, files, total = Path(root).resolve(), [], 0
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or not _name(path.name):
            raise ValueError("Invalid upload path")
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            relative = path.relative_to(root)
            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
                os.close(parent_fd)
                parent_fd = child_fd
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except (ValueError, OSError):
            raise ValueError(
                "Upload must be a regular file inside this agent profile"
            ) from None
        finally:
            os.close(parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Upload must be a regular file")
            data = stream.read(UPLOAD_BYTES - total + 1)
        total += len(data)
        if total > UPLOAD_BYTES:
            raise ValueError("Browser uploads are limited to 512 KiB total")
        files.append(
            {
                "name": path.name,
                "contentBase64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return files


@contextmanager
def _artifact_directory(profile):
    """Keep creation and failure cleanup anchored to opened profile directories."""
    profile = Path(profile)
    with ExitStack() as stack:

        def directory(path, parent=None):
            fd = os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            stack.callback(os.close, fd)
            return fd

        profile_fd = directory(profile)
        try:
            os.mkdir("downloads", mode=0o700, dir_fd=profile_fd)
        except FileExistsError:
            pass
        downloads_fd = directory("downloads", profile_fd)
        name = "desktop-browser-" + uuid.uuid4().hex
        os.mkdir(name, mode=0o700, dir_fd=downloads_fd)
        staging_fd = directory(name, downloads_fd)
        created = []
        try:
            yield profile / "downloads" / name, staging_fd, created
            # Do not publish a pathname whose directory was replaced during I/O.
            for path, parent, fd in (
                (profile, None, profile_fd),
                ("downloads", profile_fd, downloads_fd),
                (name, downloads_fd, staging_fd),
            ):
                if not os.path.samestat(
                    os.stat(path, dir_fd=parent, follow_symlinks=False), os.fstat(fd)
                ):
                    raise ValueError("Browser artifact directory changed")
        except BaseException:
            # Only unlink files this operation created, relative to the original
            # opened directory. Never follow a replacement downloads/staging link.
            for filename in created:
                try:
                    os.unlink(filename, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
            try:
                if os.path.samestat(
                    os.stat(name, dir_fd=downloads_fd, follow_symlinks=False),
                    os.fstat(staging_fd),
                ):
                    os.rmdir(name, dir_fd=downloads_fd)
            except FileNotFoundError:
                pass
            raise


def materialize(receipt, fetch_chunk, root):
    """Materialize receipt artifacts under the trusted Hermes profile root."""
    artifacts = (receipt.get("result") or {}).get("artifacts", [])
    if receipt.get("state") != "succeeded" or not artifacts:
        return receipt
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 4:
        raise ValueError("Invalid browser artifacts")
    total = 0
    for index, meta in enumerate(artifacts):
        if (
            not isinstance(meta, dict)
            or meta.get("id") != index
            or not _name(meta.get("name"))
            or type(meta.get("bytes")) is not int
            or not 0 <= meta["bytes"] <= MEDIA_BYTES
            or not isinstance(meta.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
        ):
            raise ValueError("Invalid browser artifact")
        total += meta["bytes"]
    if total > MEDIA_BYTES:
        raise ValueError("Browser media exceeds 4 MiB total")
    with _artifact_directory(root) as (staging, staging_fd, created):
        ready = []
        for meta in artifacts:
            filename = f"{meta['id']}-{meta['name']}"
            destination = staging / filename
            digest = hashlib.sha256()
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode=0o600,
                dir_fd=staging_fd,
            )
            created.append(filename)
            with os.fdopen(descriptor, "wb") as output:
                for offset in range(0, meta["bytes"], CHUNK_BYTES):
                    chunk = fetch_chunk(meta["id"], offset)
                    encoded = chunk.get("base64")
                    if (
                        chunk.get("id") != meta["id"]
                        or chunk.get("offset") != offset
                        or chunk.get("bytes") != meta["bytes"]
                        or not isinstance(encoded, str)
                        or len(encoded) > ((CHUNK_BYTES + 2) // 3) * 4
                    ):
                        raise ValueError("Invalid browser artifact chunk")
                    data = base64.b64decode(encoded, validate=True)
                    if len(data) != min(CHUNK_BYTES, meta["bytes"] - offset):
                        raise ValueError("Incomplete browser artifact")
                    output.write(data)
                    digest.update(data)
            if digest.hexdigest() != meta["sha256"]:
                raise ValueError("Browser artifact hash mismatch")
            ready.append(
                {**meta, "path": str(destination), "media": f"MEDIA:{destination}"}
            )
        return {**receipt, "result": {**receipt["result"], "artifacts": ready}}


def _http(ctx, method, path, body=None, admission=""):
    headers = {
        "Authorization": f"Bearer {ctx['hookToken']}",
        "X-Desktop-Owner": ctx["ownerId"],
        "X-Desktop-Run": ctx["runId"],
        "X-Desktop-Agent": ctx["agentId"],
        "X-Desktop-Run-Family": ctx.get("family", "room"),
        "Content-Type": "application/json",
    }
    if ctx.get("family") == "task":  # a Worker run proves its attempt, as effect_check does
        headers["X-Desktop-Task"] = ctx.get("taskId") or ""
        headers["X-Desktop-Run-Secret"] = ctx.get("runSecret") or ""
    if admission:
        headers["X-Desktop-Admission"] = admission
    request = urllib.request.Request(
        ctx["apiUrl"].rstrip("/") + "/api/v1/desktop-execution/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Desktop response exceeds limit")
    return json.loads(raw)


def submit_request(request, *, ctx, admission_token):
    request_id = str(uuid.UUID(request["requestId"]))
    # Caller retains this identity even if the HTTP response is lost; never resubmit
    # with a new UUID to recover from an unknown click/submission/upload.
    receipt_id = admission_token + "." + request_id
    try:
        return _http(ctx, "POST", "operations", request, admission_token)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            try:
                code = (
                    json.loads(exc.read(16384))
                    .get("detail", {})
                    .get("code", "desktop_request_rejected")
                )
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
                    raise ValueError()
            except (ValueError, AttributeError):
                code = "desktop_request_rejected"
            return {
                "id": receipt_id,
                "requestId": request_id,
                "state": "rejected",
                "error": {"code": code},
            }
        return {
            "id": receipt_id,
            "requestId": request_id,
            "state": "unknown",
            "error": {"code": "desktop_submit_unknown"},
        }
    except (OSError, ValueError):
        return {
            "id": receipt_id,
            "requestId": request_id,
            "state": "unknown",
            "error": {"code": "desktop_submit_unknown"},
        }


def receipt_status(receipt_id, *, ctx):
    if not isinstance(receipt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_=.-]{1,4200}", receipt_id
    ):
        raise ValueError("Invalid receipt")
    receipt = _http(ctx, "GET", "operations/" + receipt_id)
    return materialize(
        receipt,
        lambda index, offset: _http(
            ctx, "GET", f"operations/{receipt_id}/artifacts/{index}?offset={offset}"
        ),
        turn_context._home(),
    )
