/**
 * Chat attachments — pending-file model + upload rail.
 *
 * Mirrors the web composer's contract exactly (ChatInput.tsx +
 * AgentChatWindow.handleSend): files upload FIRST via
 * POST /container/files?agent_id=… (multipart, repeated field "files"), then
 * the chat message goes out with a path notice prepended so the agent knows
 * where to read them. Caps come verbatim from the backend route
 * (routers/container_files.py): 10MB per file, 10 files per request.
 */
import { createApiClient } from "@isol8/shared";

import { makeAdapter } from "./adapter";

/** 10 MB per file — lockstep with the web MAX_FILE_SIZE + backend cap. */
export const MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024;
/** Backend MAX_FILES_PER_REQUEST (the web caps its pending list the same way). */
export const MAX_ATTACHMENTS = 10;

/** A picked-but-not-yet-uploaded file (the web's PendingFile at mobile scale). */
export interface PendingAttachment {
  id: string;
  /** Display + upload filename. */
  name: string;
  /** Bytes when the picker reports it; 0 = unknown, which passes the gate. */
  size: number;
  /** Local uri RN's fetch can stream the bytes from. */
  uri: string;
  mimeType: string;
}

/** One row of the upload response — path is agent-relative (.openclaw/…). */
export interface UploadedAttachment {
  filename: string;
  path: string;
  size: number;
}

/**
 * Split candidates on the 10MB cap. Wording is the web's verbatim:
 * "a.png, b.zip exceed the 10MB limit" / "a.png exceeds the 10MB limit".
 */
export function filterOversized(candidates: PendingAttachment[]): {
  valid: PendingAttachment[];
  error: string | null;
} {
  const valid: PendingAttachment[] = [];
  const rejected: string[] = [];
  for (const c of candidates) {
    if (c.size > MAX_ATTACHMENT_SIZE) rejected.push(c.name);
    else valid.push(c);
  }
  return {
    valid,
    error:
      rejected.length > 0
        ? `${rejected.join(", ")} exceed${rejected.length === 1 ? "s" : ""} the 10MB limit`
        : null,
  };
}

/**
 * Upload pending attachments into the agent's workspace. RN has no File —
 * each FormData part is a {uri, name, type} descriptor RN's fetch streams
 * from disk (the DOM Blob cast is the standard RN typing shim). Endpoint,
 * field name, and response shape match the web's api.uploadFiles verbatim.
 */
export async function uploadAttachments(
  getToken: () => Promise<string | null>,
  agentId: string,
  attachments: PendingAttachment[],
): Promise<UploadedAttachment[]> {
  const api = createApiClient(makeAdapter(getToken));
  const form = new FormData();
  for (const a of attachments) {
    form.append("files", {
      uri: a.uri,
      name: a.name,
      type: a.mimeType,
    } as unknown as Blob);
  }
  const res = (await api.upload(
    `/container/files?agent_id=${encodeURIComponent(agentId)}`,
    form,
  )) as { uploaded: UploadedAttachment[] };
  return res.uploaded;
}

/**
 * The web's file notice, verbatim (AgentChatWindow.handleSend) — prepended
 * to the outgoing message text; no attachments field exists on the WS frame.
 */
export function buildUploadNotice(uploaded: UploadedAttachment[]): string {
  const fileList = uploaded.map((f) => `- ${f.filename} → ${f.path}`).join("\n");
  return `[The user uploaded files to your workspace. You can read them at these paths:\n${fileList}]\n\n`;
}
