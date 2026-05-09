"use client";

import { useState } from "react";
import { Folder } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { IssueProject } from "@/components/teams/shared/types";

export interface ProjectPickerProps {
  projects: IssueProject[];
  selectedProjectId: string | null;
  onChange: (projectId: string | null) => void;
  className?: string;
}

export function ProjectPicker({
  projects,
  selectedProjectId,
  onChange,
  className,
}: ProjectPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = selectedProjectId
    ? projects.find((p) => p.id === selectedProjectId)
    : null;
  const triggerLabel = selected
    ? selected.name
    : selectedProjectId
      ? "Unknown project"
      : "No project";

  const visibleProjects = query.trim()
    ? projects.filter((p) =>
        p.name.toLowerCase().includes(query.toLowerCase().trim()),
      )
    : projects;

  const handleSelect = (projectId: string | null) => {
    onChange(projectId);
    setOpen(false);
    setQuery("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-2 text-sm hover:underline",
            className,
          )}
          style={{ color: selected ? "var(--ink)" : "var(--mute)" }}
        >
          <Folder className="h-3.5 w-3.5" />
          <span>{triggerLabel}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-64 p-1"
        style={{
          background: "var(--cream)",
          border: "1px solid var(--edge)",
          color: "var(--ink)",
        }}
      >
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search projects..."
          className="w-full px-2 py-1.5 text-xs bg-transparent outline-none mb-1"
          style={{ borderBottom: "1px solid var(--edge)" }}
        />
        <ul className="max-h-48 overflow-y-auto" role="listbox">
          <li role="option" aria-selected={selectedProjectId === null}>
            <button
              type="button"
              onClick={() => handleSelect(null)}
              className="w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]"
              style={{ color: "var(--mute)" }}
            >
              Clear project
            </button>
          </li>
          {visibleProjects.map((project) => (
            <li
              key={project.id}
              role="option"
              aria-selected={selectedProjectId === project.id}
            >
              <button
                type="button"
                onClick={() => handleSelect(project.id)}
                className="w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]"
              >
                {project.name}
              </button>
            </li>
          ))}
          {visibleProjects.length === 0 && (
            <li
              className="px-2 py-2 text-xs"
              style={{ color: "var(--mute)" }}
            >
              No matches
            </li>
          )}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
