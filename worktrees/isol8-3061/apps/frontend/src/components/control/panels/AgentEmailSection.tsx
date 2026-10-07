"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { Mail } from "lucide-react";
import { ApiError } from "@lightbulb/shared";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

interface AddressRow {
  address: string;
  agent_id: string;
  verified_senders: string[];
  send_to_anyone: boolean;
  outbound_disabled: string | null;
}

// The backend raises HTTPException(detail={"status": "bad_sender", "entry": …})
// / {"status": reason} — a structured, non-string detail that @lightbulb/shared's
// extractDetailMessage doesn't promote (it only lifts a string `detail` or a
// `detail.message`), so ApiError.message falls back to the opaque "API 400".
// Read the structured body ourselves rather than touching the shared client.
function describeSaveError(e: unknown): string {
  if (e instanceof ApiError && e.body && typeof e.body === "object" && "detail" in e.body) {
    const detail = (e.body as { detail?: unknown }).detail;
    if (detail && typeof detail === "object") {
      const status = (detail as { status?: unknown }).status;
      if (status === "bad_sender") {
        const entry = (detail as { entry?: unknown }).entry;
        return `Invalid sender "${entry}" — use an address (bob@example.com) or a domain (@example.com).`;
      }
      if (typeof status === "string") return `Save failed (${status}).`;
    }
  }
  return e instanceof Error ? e.message : "Save failed";
}

export function AgentEmailSection({ agentId }: { agentId: string }) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ addresses: AddressRow[] }>(
    "/agent-mail/addresses",
    () => api.get("/agent-mail/addresses") as Promise<{ addresses: AddressRow[] }>,
    { revalidateOnFocus: false },
  );
  const row = data?.addresses.find((a) => a.agent_id === agentId);
  const [senders, setSenders] = useState("");
  const [anyone, setAnyone] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reseed local edit state only when the SERVER VALUES actually change, not
  // on every SWR revalidation — `row?.verified_senders` is a fresh array
  // reference on every fetch even when the content is identical, and `save()`
  // awaits `mutate()`, so a naive array/boolean dependency reruns this on
  // every save and clobbers whatever the user is mid-typing. Deriving
  // primitive keys first means the effect only fires on a real content
  // change, and satisfies exhaustive-deps without a lint escape hatch.
  // `agentId` is also a dep: on a deep-link retarget (AgentsPanel changes the
  // selected agent without resetting tabs) two agents can both have no row —
  // same `undefined` derived values — so without keying on agentId the
  // effect would not re-fire and the old agent's unsaved edits would carry
  // over onto the new one.
  const sendersFromRow = (row?.verified_senders ?? []).join("\n");
  const anyoneFromRow = row?.send_to_anyone ?? false;
  useEffect(() => {
    setSenders(sendersFromRow);
    setAnyone(anyoneFromRow);
  }, [agentId, row?.address, sendersFromRow, anyoneFromRow]);

  if (error && !data) return <div className="p-4 text-sm text-err">Failed to load email settings.</div>;
  if (isLoading || !data) return <div className="p-4 text-sm text-[var(--midnight-muted)]">Loading email…</div>;

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await api.patch(`/agent-mail/addresses/${agentId}`, {
        verified_senders: senders.split("\n").map((s) => s.trim()).filter(Boolean),
        send_to_anyone: anyone,
      });
      await mutate();
    } catch (e) {
      setErr(describeSaveError(e));
    } finally {
      setSaving(false);
    }
  };

  // Its own handler, not save({ outbound_disabled: null }) — save() also
  // persists whatever the user has mid-typed in the textarea/checkbox, which
  // Re-enable must not do.
  const reenable = async () => {
    setSaving(true);
    setErr(null);
    try {
      await api.patch(`/agent-mail/addresses/${agentId}`, { outbound_disabled: null });
      await mutate();
    } catch (e) {
      setErr(describeSaveError(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4 p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Mail size={16} /> Email</h3>
      <div className="rounded-md border border-[var(--edge)] p-3 space-y-3">
        <div>
          <div className="label-caps text-[var(--midnight-muted)]">Address</div>
          {row ? (
            <code className="text-sm">{row.address}</code>
          ) : (
            <p className="text-xs text-[var(--midnight-muted)]">Not created yet — it appears the first time this agent uses email, or when you save below.</p>
          )}
        </div>
        {row?.outbound_disabled && (
          <div role="alert" className="rounded-lg border border-warn bg-cream px-3 py-2 text-ui">
            Outbound email is paused: a recipient reported a message as spam.
            <Button type="button" variant="outline" size="sm" className="ml-2" disabled={saving} onClick={reenable}>
              Re-enable
            </Button>
          </div>
        )}
        <label className="block text-xs">
          <span className="font-semibold">Who can wake this agent by email</span>
          <span className="block text-[var(--midnight-muted)]">One per line: an address (bob@example.com) or a domain (@example.com). You are always allowed.</span>
          <textarea
            className="mt-1 w-full rounded-md border border-[var(--edge)] bg-transparent p-2 font-mono text-xs"
            rows={4}
            value={senders}
            disabled={saving}
            onChange={(e) => setSenders(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={anyone} disabled={saving} onChange={(e) => setAnyone(e.target.checked)} />
          Let this agent email anyone (daily limit applies; a spam report pauses all outbound)
        </label>
        {err && <p className="text-xs text-err">{err}</p>}
        <Button type="button" size="sm" disabled={saving} onClick={() => save()}>{saving ? "Saving…" : "Save"}</Button>
      </div>
    </div>
  );
}
