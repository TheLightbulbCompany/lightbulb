"use client";

import { cn } from "@/lib/utils";
import { filterAndGroupCommands, type SlashCommand } from "@/lib/slashCommands";

const CATEGORY_LABELS: Record<string, string> = {
  session: "Session", model: "Model & reasoning", tools: "Info", agents: "Agents",
};

// Built-ins grouped by category; skills + plugins collected into one labeled
// tail (commands.list has no tier, so we lean on source + the curated list).
function buildSections(commands: SlashCommand[], query: string) {
  const natives = commands.filter((c) => c.source === "native");
  const extras = commands.filter((c) => c.source !== "native");
  const sections: { label: string; items: SlashCommand[] }[] = [];
  for (const g of filterAndGroupCommands(natives, query)) {
    if (g.items.length) sections.push({ label: CATEGORY_LABELS[g.category] ?? "Info", items: g.items });
  }
  const extraGroups = filterAndGroupCommands(extras, query).flatMap((g) => g.items);
  if (extraGroups.length) sections.push({ label: "Skills", items: extraGroups });
  return sections;
}

export function flattenVisible(commands: SlashCommand[], query: string): SlashCommand[] {
  return buildSections(commands, query).flatMap((s) => s.items);
}

type SlashCommandMenuProps = {
  commands: SlashCommand[];
  query: string;
  activeIndex: number;
  onSelect: (cmd: SlashCommand) => void;
  onActiveIndexChange: (n: number) => void;
};

export function SlashCommandMenu({
  commands, query, activeIndex, onSelect, onActiveIndexChange,
}: SlashCommandMenuProps) {
  const sections = buildSections(commands, query);
  if (sections.length === 0) return null;
  // Global row index per section start, computed functionally (no render-time
  // mutation — the react-hooks/immutability rule forbids reassigning a counter
  // during render). Row order matches flattenVisible 1:1 for keyboard nav.
  const sectionStarts = sections.map((_, i) =>
    sections.slice(0, i).reduce((n, s) => n + s.items.length, 0),
  );
  return (
    <div
      role="listbox"
      className="absolute bottom-full mb-2 left-0 right-0 max-h-80 overflow-y-auto rounded-2xl border border-[var(--edge)] bg-cream shadow-lg z-20"
    >
      {sections.map((section, si) => (
        <div key={section.label} className="py-1">
          <div className="px-3 py-1 text-[0.65rem] uppercase tracking-wide text-[var(--midnight-muted)]">
            {section.label}
          </div>
          {section.items.map((cmd, ii) => {
            const here = sectionStarts[si] + ii;
            const hasArgs = cmd.acceptsArgs && !!cmd.args?.[0]?.choices?.length;
            return (
              <button
                key={cmd.name}
                role="option"
                aria-selected={here === activeIndex}
                type="button"
                onMouseEnter={() => onActiveIndexChange(here)}
                onClick={() => onSelect(cmd)}
                className={cn(
                  "w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm",
                  here === activeIndex ? "bg-[var(--midnight)]/8 text-[var(--midnight)]" : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)]",
                )}
              >
                <span className="font-medium text-[var(--midnight)]">/{cmd.name}</span>
                <span className="flex-1 truncate text-[var(--midnight-muted)]">{cmd.description}</span>
                {hasArgs && <span className="text-[var(--midnight-faint)]">▸</span>}
              </button>
            );
          })}
        </div>
      ))}
      <div className="px-3 py-1 text-[0.65rem] text-[var(--midnight-muted)] border-t border-[var(--edge)]">
        ↑↓ navigate · Tab fill · ↵ run · esc close
      </div>
    </div>
  );
}
