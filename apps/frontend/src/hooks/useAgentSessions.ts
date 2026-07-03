"use client";

import { useMemo } from "react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { notifyApiError } from "@/lib/notify";

// Mirror of the backend guard (websocket_chat.py _SESSION_ID_RE): a strict
// lowercase RFC-4122 v4. Anything else in the trailing segment is not a
// session we minted and must never be surfaced as selectable.
const SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface AgentSession {
  /** Trailing UUID for a real session; null == the legacy "General" session. */
  sessionId: string | null;
  /** The full OpenClaw session key. */
  key: string;
  /** Display label — truncated to 48 chars. Seed edits from rawLabel. */
  label: string;
  /** Untruncated source of `label`; what a rename editor must start from. */
  rawLabel: string;
  updatedAt?: number;
  totalTokens?: number;
  // Per-session model/effort overrides sessions.list reports (all optional —
  // an unset session inherits the agent/config defaults). The composer pill
  // hydrates from these so it reflects persisted state rather than a stub.
  /** Persisted model, either a full `${provider}/${id}` ref or a bare id. */
  model?: string;
  /** Provider for the persisted model, when reported separately from `model`. */
  modelProvider?: string;
  /** Persisted reasoning/effort override (e.g. "high"); absent == inherit. */
  thinkingLevel?: string;
  /** Persisted fast-mode toggle (BYO-OpenAI). */
  fastMode?: boolean;
}

interface RawSessionRow {
  key: string;
  label?: string;
  derivedTitle?: string;
  title?: string;
  lastMessage?: { text?: string };
  updatedAt?: number;
  totalTokens?: number;
  model?: string;
  modelProvider?: string;
  thinkingLevel?: string;
  fastMode?: boolean;
}

/**
 * Defensive extraction of the per-session model/effort overrides. sessions.list
 * rows are untyped over the wire, so each field is type-guarded and left
 * `undefined` when absent or of the wrong shape (== inherit the default).
 */
function modelControlsOf(row: RawSessionRow): Pick<
  AgentSession,
  "model" | "modelProvider" | "thinkingLevel" | "fastMode"
> {
  return {
    model: typeof row.model === "string" ? row.model : undefined,
    modelProvider: typeof row.modelProvider === "string" ? row.modelProvider : undefined,
    thinkingLevel: typeof row.thinkingLevel === "string" ? row.thinkingLevel : undefined,
    fastMode: typeof row.fastMode === "boolean" ? row.fastMode : undefined,
  };
}

function labelsFor(
  row: RawSessionRow,
  sessionId: string,
): { label: string; rawLabel: string } {
  // A user-set label (sessions.patch {label}) always wins over derived titles.
  const raw = (row.label || row.derivedTitle || row.title || row.lastMessage?.text || "").trim();
  // Never blank — fall back to a short id so the row is always clickable.
  const rawLabel = raw || `Session ${sessionId.slice(0, 8)}`;
  // Display copy is truncated; rawLabel stays intact so a rename editor never
  // starts from (and then persists) an ellipsis-truncated string.
  const label = rawLabel.length > 48 ? `${rawLabel.slice(0, 48)}…` : rawLabel;
  return { label, rawLabel };
}

/**
 * Per-agent session list for the sidebar. The backend sessions.list proxy has
 * already dropped paperclip/cron/channel/other-member rows; here we keep only
 * 4-part `agent:{agentId}:{userId}:{uuid}` rows for THIS agent + user, newest
 * first. The legacy General (3-part) row is added by useAgentSessions' Task-12
 * extension.
 */
export function useAgentSessions(
  agentId: string | null,
  userId: string | null,
): {
  sessions: AgentSession[];
  isLoading: boolean;
  mutate: () => Promise<unknown>;
  renameSession: (key: string, label: string) => Promise<void>;
  deleteSession: (key: string) => Promise<void>;
} {
  const callRpc = useGatewayRpcMutation();
  const { data, isLoading, mutate } = useGatewayRpc<
    { sessions?: RawSessionRow[] } | RawSessionRow[]
  >(
    agentId ? "sessions.list" : null,
    {
      includeGlobal: false,
      includeUnknown: false,
      includeDerivedTitles: true,
      includeLastMessage: true,
      limit: 50,
    },
    { dedupingInterval: 10_000 },
  );

  const sessions = useMemo<AgentSession[]>(() => {
    if (!agentId || !userId) return [];
    // OpenClaw lowercases the whole session key on store/lookup, so sessions.list
    // returns lowercased keys. Compare the agent/user segments case-insensitively
    // — a mixed-case Clerk user id (the common case) would otherwise mismatch
    // every row, rendering an empty session list and never hydrating the pill.
    const agentIdL = agentId.toLowerCase();
    const userIdL = userId.toLowerCase();
    const rows: RawSessionRow[] = Array.isArray(data) ? data : data?.sessions ?? [];
    const out: AgentSession[] = [];
    for (const row of rows) {
      if (typeof row?.key !== "string") continue;
      const parts = row.key.split(":");
      if (parts.length !== 4) continue;
      if (parts[0] !== "agent" || parts[1].toLowerCase() !== agentIdL || parts[2].toLowerCase() !== userIdL)
        continue;
      const sessionId = parts[3];
      if (!SESSION_ID_RE.test(sessionId)) continue;
      out.push({
        sessionId,
        key: row.key,
        ...labelsFor(row, sessionId),
        updatedAt: row.updatedAt,
        totalTokens: row.totalTokens,
        ...modelControlsOf(row),
      });
    }
    // Surface the legacy 3-part conversation as a normal "General" session so
    // no pre-sessions history is orphaned. Match case-insensitively for the same
    // reason as above; the backend proxy has already scoped rows to this user.
    const legacyKey = `agent:${agentIdL}:${userIdL}`;
    const generalRow = rows.find(
      (row) => typeof row?.key === "string" && row.key.toLowerCase() === legacyKey,
    );

    out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    if (generalRow) {
      out.unshift({
        sessionId: null,
        key: generalRow.key,
        label: "General",
        rawLabel: "General",
        ...modelControlsOf(generalRow),
      });
    }
    return out;
  }, [data, agentId, userId]);

  const renameSession = async (key: string, label: string): Promise<void> => {
    // sessions.patch {label} is OpenClaw's user-set session name; it wins over
    // derivedTitle in labelFor. Failures surface as a toast — the inline
    // editor has already closed by the time the RPC settles, so silence would
    // read as a successful rename.
    try {
      await callRpc("sessions.patch", { key, label });
    } catch (err) {
      notifyApiError(err, "Failed to rename session", { dedupeKey: "session-rename" });
      return;
    }
    // The patch persisted; a failed revalidation only delays the sidebar
    // refresh (SWR retries on focus) and must not read as a failed rename.
    try {
      await mutate();
    } catch {
      // revalidates on next focus
    }
  };

  const deleteSession = async (key: string): Promise<void> => {
    // sessions.delete drops the session entry; deleteTranscript archives its
    // transcript with it (matching the Sessions panel's delete semantics).
    // Failures propagate — the confirm dialog is still open and shows them
    // inline, unlike rename's fire-and-forget toast.
    await callRpc("sessions.delete", { key, deleteTranscript: true });
    // The delete persisted; a failed revalidation only delays the sidebar
    // refresh (SWR retries on focus) and must not read as a failed delete.
    try {
      await mutate();
    } catch {
      // revalidates on next focus
    }
  };

  return { sessions, isLoading, mutate, renameSession, deleteSession };
}
