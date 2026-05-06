"use client";

import { useState, useEffect } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface CompanySettings {
  display_name?: string;
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
      setName(data.display_name ?? "");
      setDesc(data.description ?? "");
    }
  }, [data]);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      // SECURITY: body contains ONLY {display_name, description}, matching
      // PatchCompanySettingsBody (Task 3, extra="forbid"). Adapter, plugin,
      // and instance settings are operator-controlled — defense in depth.
      await patch("/settings", { display_name: name, description: desc });
      mutate();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="editorial-page p-8 max-w-2xl space-y-4">
      <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Settings</h1>
      <label className="block">
        <span className="text-sm text-[var(--ink-soft)]">Display name</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
        />
      </label>
      <label className="block">
        <span className="text-sm text-[var(--ink-soft)]">Description</span>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          rows={4}
        />
      </label>
      {err && (
        <div className="text-[var(--danger)] text-sm border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
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
      <p className="text-xs text-[var(--mute)] pt-4 border-t border-[var(--edge)] italic">
        Adapter, plugin, and instance settings are operator-controlled and not
        editable here.
      </p>
    </div>
  );
}
