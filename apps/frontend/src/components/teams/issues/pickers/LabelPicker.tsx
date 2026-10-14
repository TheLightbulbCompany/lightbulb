"use client";

import { useMemo, useState } from "react";
import { Check, Plus, X } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { cn } from "@/lib/utils";
import type { IssueLabel } from "@/components/teams/shared/types";

// Shared chip style so the selected labels read identically to the rest of the
// rail's pill rows (Related Tasks, blocker chips).
const CHIP_STYLE = {
  background: "var(--cream-deep)",
  color: "var(--midnight-soft)",
  border: "1px solid var(--edge)",
} as const;

export interface LabelPickerProps {
  /** Currently-applied labels (the denormalized list off the issue). */
  selected: IssueLabel[];
  /** Receives the FULL desired label-id set (replace semantics — the whole
   * array is sent, matching the BFF's ``labelIds`` PATCH contract). */
  onChange: (labelIds: string[]) => void;
  className?: string;
}

/** Editable Labels rail control: a themed multi-select popover over the
 * company's label set. Toggling an option sends the full desired id set so the
 * server replaces the issue's labels wholesale. Renders the applied labels as
 * removable chips next to an "Add label" trigger. */
export function LabelPicker({ selected, onChange, className }: LabelPickerProps) {
  const { read, post } = useTeamsApi();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);

  // Company labels for the dropdown. There's no server-side label search on
  // the Teams BFF, so we filter the full list client-side (mirrors how
  // IssueRelations filters its candidate issues).
  const { data, mutate } = read<{ labels: IssueLabel[] }>("/issues/labels");
  const allLabels = data?.labels;

  const selectedIds = useMemo(() => selected.map((l) => l.id), [selected]);

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (allLabels ?? [])
      .filter((label) => (q ? label.name.toLowerCase().includes(q) : true))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [allLabels, query]);

  // Full-array replace, exactly like the blocker picker's toggle.
  const toggle = (labelId: string) => {
    const next = selectedIds.includes(labelId)
      ? selectedIds.filter((id) => id !== labelId)
      : [...selectedIds, labelId];
    onChange(next);
  };
  const remove = (labelId: string) => {
    onChange(selectedIds.filter((id) => id !== labelId));
  };

  // Nothing seeds labels, so without a create-here path this picker is empty
  // on every fresh company and there is no other screen that fills it. The
  // typed query becomes the label name; the BFF derives the colour.
  const trimmedQuery = query.trim();
  const canCreate =
    trimmedQuery.length > 0 &&
    trimmedQuery.length <= 48 &&
    !(allLabels ?? []).some(
      (l) => l.name.toLowerCase() === trimmedQuery.toLowerCase(),
    );

  const createAndApply = async () => {
    if (!canCreate || creating) return;
    setCreating(true);
    try {
      const created = await post<IssueLabel>("/issues/labels", {
        name: trimmedQuery,
      });
      await mutate();
      onChange([...selectedIds, created.id]);
      setQuery("");
    } catch (e) {
      notifyApiError(e, "Couldn't create the label.");
    } finally {
      setCreating(false);
    }
  };

  return (
    <span className={cn("flex flex-wrap items-center gap-1", className)}>
      {selected.map((label) => (
        <span
          key={label.id}
          className="group inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-caption"
          style={CHIP_STYLE}
        >
          <span className="truncate">{label.name}</span>
          <button
            type="button"
            className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full opacity-0 transition-opacity duration-(--dur-fast) ease-out group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
            style={{ color: "var(--midnight-muted)" }}
            aria-label={`Remove label ${label.name}`}
            title={`Remove label ${label.name}`}
            onClick={() => remove(label.id)}
          >
            <X size={12} strokeWidth={1.75} />
          </button>
        </span>
      ))}
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setQuery("");
        }}
      >
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-caption transition-colors hover:underline"
            style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
          >
            <Plus size={12} strokeWidth={1.75} />
            Add label
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-64 p-1"
          style={{
            background: "var(--cream)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        >
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              // Enter creates when the typed name is new — the gesture people
              // expect from a tag input, and the only path on an empty company.
              if (e.key === "Enter" && canCreate) {
                e.preventDefault();
                void createAndApply();
              }
            }}
            placeholder="Search or create labels..."
            className="w-full px-2 py-1.5 text-caption bg-transparent mb-1 rounded-sm"
            style={{ borderBottom: "1px solid var(--edge)" }}
            aria-label="Search labels"
          />
          <ul className="max-h-48 overflow-y-auto" role="listbox" aria-multiselectable="true">
            {candidates.map((label) => {
              const isSelected = selectedIds.includes(label.id);
              return (
                <li key={label.id} role="option" aria-selected={isSelected}>
                  <button
                    type="button"
                    onClick={() => toggle(label.id)}
                    className={cn(
                      "flex w-full items-center gap-2 px-2 py-1.5 text-left text-ui rounded-sm hover:bg-[var(--cream-deep)]",
                      isSelected && "bg-[var(--cream-deeper)]",
                    )}
                  >
                    <span className="truncate">{label.name}</span>
                    {isSelected && (
                      <Check size={16}
                        className="ml-auto shrink-0"
                        aria-hidden="true"
                      />
                    )}
                  </button>
                </li>
              );
            })}
            {candidates.length === 0 && !canCreate && (
              <li className="px-2 py-2 text-caption" style={{ color: "var(--midnight-muted)" }}>
                {trimmedQuery ? "No matching labels." : "No labels yet — type to create one."}
              </li>
            )}
            {canCreate && (
              <li>
                <button
                  type="button"
                  onClick={() => void createAndApply()}
                  disabled={creating}
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-ui rounded-sm hover:bg-[var(--cream-deep)]"
                >
                  <Plus size={16} className="shrink-0" aria-hidden="true" />
                  <span className="truncate">
                    {creating ? "Creating…" : `Create "${trimmedQuery}"`}
                  </span>
                </button>
              </li>
            )}
          </ul>
        </PopoverContent>
      </Popover>
    </span>
  );
}
