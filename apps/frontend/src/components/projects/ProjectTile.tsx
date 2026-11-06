"use client";

// A project's identity (UX slice 6): one of 12 glyphs on one of 6 quiet colours, or, before either is picked, the
// name's first letter on a neutral tile. 16 in the sidebar, 24 in the header, 32 on Home, 20 in a picker field.

import type { ReactElement } from "react";
import { Briefcase, Calendar, ChartLine, ChevronDown, Code, Leaf, Megaphone, Package, PenLine, Rocket, ShoppingBag, Sparkles, Users, type LucideIcon } from "lucide-react";
import { PROJECT_COLORS, PROJECT_ICONS, type ProjectColor, type ProjectIcon } from "@lightbulb/shared";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import "./projects.css";

const GLYPHS: Record<ProjectIcon, LucideIcon> = {
  sparkles: Sparkles, rocket: Rocket, "shopping-bag": ShoppingBag, megaphone: Megaphone, code: Code, "pen-line": PenLine,
  "chart-line": ChartLine, users: Users, calendar: Calendar, package: Package, leaf: Leaf, briefcase: Briefcase,
};
/** Spoken names for the picker (a glyph alone has none). */
const GLYPH_NAMES: Record<ProjectIcon, string> = {
  sparkles: "Sparkles", rocket: "Rocket", "shopping-bag": "Shopping bag", megaphone: "Megaphone", code: "Code", "pen-line": "Pen",
  "chart-line": "Chart", users: "People", calendar: "Calendar", package: "Package", leaf: "Leaf", briefcase: "Briefcase",
};
const COLOR_NAMES = ["Lavender", "Teal", "Sand", "Rose", "Olive", "Slate"] as const;

export interface ProjectIdentity { icon?: ProjectIcon | null; color?: ProjectColor | null }

export function ProjectTile({ icon, color, name, size }: ProjectIdentity & { name: string; size: 16 | 20 | 24 | 32 }): ReactElement {
  const Glyph = icon ? GLYPHS[icon] : null;
  return <span aria-hidden="true" className="project-tile" data-size={size} data-color={color ?? undefined}>
    {Glyph ? <Glyph /> : (name.trim()[0] ?? "#").toUpperCase()}
  </span>;
}

/** The tile as a button that opens 12 glyphs and 6 colours; each pick saves at once. */
export function ProjectIconPicker({ icon, color, name, onChange, disabled, className }: ProjectIdentity & {
  name: string; onChange: (next: ProjectIdentity) => void; disabled?: boolean; className?: string;
}): ReactElement {
  return <Popover>
    <PopoverTrigger asChild>
      <button type="button" className={cn("project-picker-trigger", className)} disabled={disabled} aria-label="Project icon and colour">
        <ProjectTile icon={icon} color={color} name={name} size={20} /><ChevronDown size={12} aria-hidden="true" />
      </button>
    </PopoverTrigger>
    <PopoverContent align="start" className="project-picker">
      <div role="group" aria-label="Icon" className="project-picker__grid">
        {PROJECT_ICONS.map((g) => {
          const Glyph = GLYPHS[g];
          return <button key={g} type="button" aria-label={GLYPH_NAMES[g]} aria-pressed={icon === g}
            onClick={() => onChange({ icon: icon === g ? null : g, color })}><Glyph aria-hidden="true" /></button>;
        })}
      </div>
      <div role="group" aria-label="Colour" className="project-picker__grid">
        {PROJECT_COLORS.map((c, i) => <button key={c} type="button" aria-label={COLOR_NAMES[i]} aria-pressed={color === c}
          onClick={() => onChange({ icon, color: color === c ? null : c })}><span className="project-picker__swatch" data-color={c} /></button>)}
      </div>
    </PopoverContent>
  </Popover>;
}
