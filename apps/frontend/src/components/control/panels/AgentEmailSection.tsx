"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { Mail } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

interface AddressRow {
  address: string;
  agent_id: string;
  verified_senders: string[];
  send_to_anyone: boolean;
  outbound_disabled: string | null;
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

  useEffect(() => {
    setSenders((row?.verified_senders ?? []).join("\n"));
    setAnyone(row?.send_to_anyone ?? false);
  }, [row?.address, row?.verified_senders, row?.send_to_anyone]);

  if (error && !data) return <div className="p-4 text-sm text-[#dc2626]">Failed to load email settings.</div>;
  if (isLoading || !data) return <div className="p-4 text-sm text-[var(--mute)]">Loading email…</div>;

  const save = async (extra: Record<string, unknown> = {}) => {
    setSaving(true);
    setErr(null);
    try {
      await api.patch(`/agent-mail/addresses/${agentId}`, {
        verified_senders: senders.split("\n").map((s) => s.trim()).filter(Boolean),
        send_to_anyone: anyone,
        ...extra,
      });
      await mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4 p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Mail className="h-4 w-4" /> Email</h3>
      <div className="rounded-md border border-[var(--edge)] p-3 space-y-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-[var(--mute)]">Address</div>
          {row ? (
            <code className="text-sm">{row.address}</code>
          ) : (
            <p className="text-xs text-[var(--mute)]">Not created yet — it appears the first time this agent uses email, or when you save below.</p>
          )}
        </div>
        {row?.outbound_disabled && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs">
            Outbound email is paused: a recipient reported a message as spam.
            <Button variant="outline" size="sm" className="ml-2" disabled={saving} onClick={() => save({ outbound_disabled: null })}>
              Re-enable
            </Button>
          </div>
        )}
        <label className="block text-xs">
          <span className="font-semibold">Who can wake this agent by email</span>
          <span className="block text-[var(--mute)]">One per line: an address (bob@example.com) or a domain (@example.com). You are always allowed.</span>
          <textarea
            className="mt-1 w-full rounded-md border border-[var(--edge)] bg-transparent p-2 font-mono text-xs"
            rows={4}
            value={senders}
            onChange={(e) => setSenders(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={anyone} onChange={(e) => setAnyone(e.target.checked)} />
          Let this agent email anyone (daily limit applies; a spam report pauses all outbound)
        </label>
        {err && <p className="text-xs text-[#dc2626]">{err}</p>}
        <Button size="sm" disabled={saving} onClick={() => save()}>{saving ? "Saving…" : "Save"}</Button>
      </div>
    </div>
  );
}
