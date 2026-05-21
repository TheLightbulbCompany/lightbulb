"use client";

import {
  PAPERCLIP_AGENT_ROLES,
  PAPERCLIP_ROLE_LABELS,
  type PaperclipMeta,
  type PaperclipRole,
} from "@/lib/paperclipMeta";

interface AgentPaperclipFieldsProps {
  value: PaperclipMeta;
  onChange: (next: PaperclipMeta) => void;
  disabled?: boolean;
}

/**
 * Shared role/title/capabilities form. Used in:
 *   * AgentCreateForm — when creating a new agent.
 *   * AgentOverviewTab — when editing an existing agent.
 *   * (Future) PublishWizard — for sellers to override per-listing.
 *
 * The same values write into agent_entry.params.paperclip via the
 * backend's paperclip_meta module, so the agent's profile in
 * Paperclip stays in sync without the user thinking about the split.
 */
export function AgentPaperclipFields({
  value,
  onChange,
  disabled,
}: AgentPaperclipFieldsProps) {
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground" htmlFor="paperclip-role">
          Role
        </label>
        <select
          id="paperclip-role"
          value={value.role}
          disabled={disabled}
          onChange={(e) =>
            onChange({ ...value, role: e.target.value as PaperclipRole })
          }
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        >
          {PAPERCLIP_AGENT_ROLES.map((r) => (
            <option key={r} value={r}>
              {PAPERCLIP_ROLE_LABELS[r]}
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-1">
        <label className="text-xs text-muted-foreground" htmlFor="paperclip-title">
          Title <span className="text-muted-foreground/60">(short)</span>
        </label>
        <input
          id="paperclip-title"
          type="text"
          value={value.title ?? ""}
          disabled={disabled}
          maxLength={80}
          placeholder="e.g. Restaurant booking specialist"
          onChange={(e) => onChange({ ...value, title: e.target.value })}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
      </div>

      <div className="space-y-1">
        <label
          className="text-xs text-muted-foreground"
          htmlFor="paperclip-capabilities"
        >
          Capabilities{" "}
          <span className="text-muted-foreground/60">
            (what this agent does — Friday reads this)
          </span>
        </label>
        <textarea
          id="paperclip-capabilities"
          value={value.capabilities ?? ""}
          disabled={disabled}
          maxLength={1024}
          rows={3}
          placeholder="e.g. Books Resy reservations. Knows my preferences from USER.md."
          onChange={(e) => onChange({ ...value, capabilities: e.target.value })}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
      </div>
    </div>
  );
}
