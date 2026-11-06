"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Check, Copy, Mail } from "lucide-react";
import { ApiError } from "@lightbulb/shared";

import { Button } from "@/components/ui/button";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApi } from "@/lib/api";
import { navigateShell } from "@/lib/shellNav";

interface AddressRow {
  address: string;
  verified_senders: string[];
  send_to_anyone: boolean;
  footer_enabled: boolean;
  outbound_disabled: string | null;
}

// The backend raises HTTPException(detail={"status": "bad_sender", "entry": …})
// / {"status": reason} — a structured, non-string detail that @lightbulb/shared's
// extractDetailMessage doesn't promote (it only lifts a string `detail` or a
// `detail.message`), so ApiError.message falls back to the opaque "API 400".
// Read the structured body ourselves rather than touching the shared client.
const ADMIN_ONLY = "Only a workspace admin can change this.";

function describeSaveError(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) return ADMIN_ONLY;
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

export function AgentEmailSection() {
  const api = useApi();
  const canManage = useIsWorkspaceAdmin();
  const { data: row, error, isLoading, mutate } = useSWR<AddressRow>(
    "/agent-mail/address",
    () => api.get("/agent-mail/address") as Promise<AddressRow>,
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const [copied, setCopied] = useState(false);
  const [senders, setSenders] = useState("");
  const [anyone, setAnyone] = useState(true);
  const [footerEnabled, setFooterEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reseed local edit state only when the SERVER VALUES actually change, not on every SWR
  // revalidation: `verified_senders` is a fresh array each fetch, and save() awaits mutate(),
  // so depending on it would clobber whatever the user is mid-typing.
  const sendersFromRow = (row?.verified_senders ?? []).join("\n");
  const anyoneFromRow = row?.send_to_anyone ?? true;
  const footerFromRow = row?.footer_enabled ?? true;
  useEffect(() => {
    setSenders(sendersFromRow);
    setAnyone(anyoneFromRow);
    setFooterEnabled(footerFromRow);
  }, [row?.address, sendersFromRow, anyoneFromRow, footerFromRow]);

  const copyTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(copyTimer.current), []);
  const copy = async () => {
    if (!row || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(row.address);
    } catch {
      return;
    }
    setCopied(true);
    clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied(false), 1500);
  };

  if (error instanceof ApiError && error.status === 409) {
    return <div className="p-4 text-sm text-[var(--midnight-muted)]">Your workspace address is being set up — it appears once your company name is set.</div>;
  }
  if (error && !row) return <div className="p-4 text-sm text-err">Failed to load email settings.</div>;
  if (isLoading || !row) return <div className="p-4 text-sm text-[var(--midnight-muted)]">Loading email…</div>;

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const list = senders.split("\n").map((s) => s.trim()).filter(Boolean);
      await api.patch("/agent-mail/address", {
        verified_senders: list,
        send_to_anyone: anyone,
        footer_enabled: footerEnabled,
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
      await api.patch("/agent-mail/address", { outbound_disabled: null });
      await mutate();
    } catch (e) {
      setErr(describeSaveError(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4 p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Mail size={16} /> Team email</h3>
      <div className="rounded-md border border-[var(--edge)] p-3 space-y-3">
        <div>
          <div className="flex items-center gap-2">
            <code className="text-sm font-mono">{row.address}</code>
            <Button type="button" variant="ghost" size="sm" aria-label="Copy address" onClick={copy}>
              {copied ? <><Check size={14} className="mr-1" /> Copied</> : <Copy size={14} />}
            </Button>
          </div>
          <p className="mt-1 text-xs text-[var(--midnight-muted)]">Add this address to a calendar event to bring the team into the meeting. Mail to it goes to Lightbulb; replies go back to whichever agent wrote first.</p>
        </div>
        {/* Mail and meetings live in their own panel (the rail's Mail). */}
        <Button type="button" variant="outline" size="sm" onClick={() => navigateShell("/chat?panel=mail")}>
          <Mail size={14} aria-hidden="true" /> Open Mail
        </Button>
        {row.outbound_disabled && (
          <div role="alert" className="rounded-lg border border-warn bg-cream px-3 py-2 text-ui">
            Outbound email is paused: a recipient reported a message as spam.
            {canManage && (
              <Button type="button" variant="outline" size="sm" className="ml-2" disabled={saving} onClick={reenable}>
                Re-enable
              </Button>
            )}
          </div>
        )}
        <label className="block text-xs">
          <span className="font-semibold">Who can email the team</span>
          <span className="block text-[var(--midnight-muted)]">One per line: an address (bob@example.com) or a domain (@example.com). You are always allowed. Applies when open email is off; while it is on, anyone whose mail passes authentication can email the team.</span>
          <textarea aria-label="Who can email the team"
            className="mt-1 w-full rounded-md border border-[var(--edge)] bg-transparent p-2 font-mono text-xs"
            rows={4}
            value={senders}
            disabled={saving || !canManage}
            onChange={(e) => setSenders(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={anyone} disabled={saving || !canManage} onChange={(e) => setAnyone(e.target.checked)} />
          Open email: anyone can email the team and agents can email anyone (daily limit applies; a spam report pauses all outbound)
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={footerEnabled} disabled={saving || !canManage} onChange={(e) => setFooterEnabled(e.target.checked)} />
          Add a Lightbulb attribution footer to agent email
        </label>
        {err && <p className="text-xs text-err">{err}</p>}
        {canManage
          ? <Button type="button" size="sm" disabled={saving} onClick={() => save()}>{saving ? "Saving…" : "Save"}</Button>
          : <p className="text-xs text-[var(--midnight-muted)]">{ADMIN_ONLY}</p>}
      </div>
    </div>
  );
}
