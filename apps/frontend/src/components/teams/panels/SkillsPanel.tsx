"use client";

import { useState, useMemo } from "react";
import { Sparkles } from "lucide-react";
import {
  PanelMasthead,
  PanelToolbar,
  PanelEmptyState,
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Skill {
  id: string;
  name: string;
  description?: string;
  version?: string;
}

export function SkillsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ skills: Skill[] }>("/skills");
  const [search, setSearch] = useState("");

  const skills = data?.skills;
  const filtered = useMemo(() => {
    const list = skills ?? [];
    return search
      ? list.filter((s) =>
          (s.name + (s.description ?? ""))
            .toLowerCase()
            .includes(search.toLowerCase()),
        )
      : list;
  }, [skills, search]);

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  const columns: PanelDataColumn<Skill>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      cell: (s) => (
        <div className="font-medium text-[var(--ink)]">{s.name}</div>
      ),
    },
    {
      key: "description",
      header: "Description",
      cell: (s) => (
        <span className="text-sm text-[var(--ink-soft)]">
          {s.description ?? "—"}
        </span>
      ),
    },
    {
      key: "version",
      header: "Version",
      align: "right",
      cell: (s) =>
        s.version ? (
          <span className="font-mono text-xs text-[var(--mute)]">
            v{s.version}
          </span>
        ) : (
          <span className="text-[var(--mute)]">—</span>
        ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Capabilities" title="Skills" />
      <p className="-mt-3 font-editorial-serif text-xs italic text-[var(--mute)]">
        Skills are operator-curated. To add or modify, contact your administrator.
      </p>
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find skill..."
        searchAriaLabel="Search skills"
      />
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Sparkles}
          headline="No skills available."
          supporting={
            search
              ? "No skills match your search."
              : "Skills extend what your agents can do. Talk to your administrator about enabling skills for your team."
          }
        />
      ) : (
        <PanelDataTable<Skill>
          rows={filtered}
          columns={columns}
          rowKey={(s) => s.id}
        />
      )}
    </div>
  );
}
