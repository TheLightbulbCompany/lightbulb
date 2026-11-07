"use client";

import { useState, useEffect } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

// GET /settings returns the workspace's `name` and `description` (the META row,
// name falling back to the Clerk org name); PATCH accepts only those two keys.
interface CompanySettings {
  name?: string;
  description?: string;
}

export function SettingsPanel() {
  const { read, patch } = useTeamsApi();
  const { data, mutate } = read<CompanySettings>("/settings");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (data) {
      setName(data.name ?? "");
      setDesc(data.description ?? "");
    }
  }, [data]);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      // SECURITY: body contains ONLY {name, description}, matching
      // PatchCompanySettingsBody (extra="forbid"). Adapter, plugin, and
      // instance settings are operator-controlled — defense in depth.
      await patch("/settings", { name, description: desc });
      mutate();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    // The Workspace section of the Settings page, which supplies the heading.
    <div className="max-w-2xl space-y-4">
      <label className="block">
        <span className="text-ui text-[var(--midnight-soft)]">Display name</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
        />
      </label>
      <label className="block">
        <span className="text-ui text-[var(--midnight-soft)]">Description</span>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          rows={4}
        />
      </label>
      {err && (
        <div className="text-err text-ui border border-err bg-cream rounded-lg px-3 py-2">
          {err}
        </div>
      )}
      <button
        onClick={save}
        disabled={saving}
        className="editorial-btn editorial-btn--primary"
      >
        {saving ? "Saving…" : "Save"}
      </button>
      <p className="text-caption text-[var(--midnight-muted)] pt-4 border-t border-[var(--edge)] italic">
        Adapter, plugin, and instance settings are operator-controlled and not
        editable here.
      </p>
    </div>
  );
}
