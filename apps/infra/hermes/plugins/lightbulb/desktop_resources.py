"""Files and scoped MCP on one explicitly selected Mac. No transport replay.

Task 7 registers these helpers; descriptors remain untrusted tool output. Directory
archives are downloaded as files, never automatically extracted into the profile.
"""

import uuid

from . import desktop_transport


def _submit(
    device,
    generation,
    command,
    params,
    *,
    ctx,
    admission_token,
    request_id,
    execution_deadline_ms=30000,
):
    return desktop_transport.submit_request(
        {
            "requestId": str(uuid.UUID(request_id)),
            "runId": ctx["runId"],
            "agentId": ctx["agentId"],
            "deviceId": device,
            "expectedConnectionGeneration": generation,
            "command": command,
            "params": params,
            "executionDeadlineMs": execution_deadline_ms,
        },
        ctx=ctx,
        admission_token=admission_token,
    )


def file_action(device, generation, action, args, **context):
    if action not in {"file.fetch", "file.write", "dir.list", "dir.fetch"}:
        raise ValueError("Unsupported file action")
    params = dict(args)
    if action == "file.write":
        source = params.pop("sourcePath")
        data = desktop_transport.upload_files(
            [source], desktop_transport.turn_context._home()
        )[0]
        params |= {"contentBase64": data["contentBase64"], "sha256": data["sha256"]}
    return _submit(device, generation, action, params, **context)


def mcp_list(device, generation, **context):
    return _submit(device, generation, "mcp_list", {}, **context)


def mcp_call(device, generation, server, tool, arguments, catalog_version, **context):
    return _submit(
        device,
        generation,
        "mcp_call",
        {
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "catalogVersion": catalog_version,
        },
        **context,
    )


receipt_status = desktop_transport.receipt_status
