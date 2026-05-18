import type { Listing } from "@/lib/marketplace/types";
import type { Agent as InstalledAgent } from "@/hooks/useAgents";
import { agentDisplayName, getAgentModelString } from "@/hooks/useAgents";

export interface EditorialIdentity {
  monogram: string;
  accent: string;
  tint: string;
}

export interface EditorialListing extends EditorialIdentity {
  slug: string;
  name: string;
  role: string;
  vibe: string;
  skills: string[];
  price: string;
  isFree: boolean;
  tags: string[];
}

const PALETTE: ReadonlyArray<{ accent: string; tint: string }> = [
  { accent: "#06402B", tint: "#e6efe9" },
  { accent: "#3a4a8a", tint: "#e8eaf3" },
  { accent: "#8a3a3a", tint: "#f3e8e8" },
  { accent: "#a35d1f", tint: "#f3ebe0" },
  { accent: "#1f5a7a", tint: "#e0ecf1" },
  { accent: "#7a3a5a", tint: "#f1e6ec" },
  { accent: "#2d6b4e", tint: "#e3eee7" },
  { accent: "#5a3a8a", tint: "#ebe6f3" },
  { accent: "#4a4a4a", tint: "#ececec" },
  { accent: "#5a2a2a", tint: "#efe2e2" },
];

export function initialsFor(value: string): string {
  const parts = value.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  const stripped = value.trim();
  if (stripped.length >= 2) return stripped.slice(0, 2);
  return (stripped + "Ag").slice(0, 2);
}

export function paletteFor(slug: string): { accent: string; tint: string } {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = (hash * 31 + slug.charCodeAt(i)) | 0;
  }
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/^[#>*\-+]+\s*/gm, "")
    .replace(/\*\*|__|\*|_/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function firstSentence(text: string, max = 140): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const match = trimmed.match(/^.+?[.!?](\s|$)/);
  const candidate = match ? match[0] : trimmed;
  return candidate.length > max ? candidate.slice(0, max - 1).trimEnd() + "…" : candidate.trim();
}

export function fromListing(listing: Listing): EditorialListing {
  const palette = paletteFor(listing.slug);
  const monogram = initialsFor(listing.name);
  const vibe = firstSentence(stripMarkdown(listing.description_md)) || "An Isol8 marketplace agent.";
  const role = listing.tags[0] ? listing.tags[0] : "Agent";
  const isFree = listing.price_cents === 0;
  const price = isFree ? "Free" : `$${(listing.price_cents / 100).toFixed(2)}`;
  return {
    slug: listing.slug,
    name: listing.name,
    role,
    monogram,
    accent: palette.accent,
    tint: palette.tint,
    vibe,
    skills: listing.tags,
    price,
    isFree,
    tags: listing.tags,
  };
}

export function fromInstalledAgent(agent: InstalledAgent): EditorialIdentity & {
  slug: string;
  name: string;
  role: string;
} {
  const name = agentDisplayName(agent);
  const palette = paletteFor(agent.id);
  const monogram = initialsFor(name);
  const model = getAgentModelString(agent)?.split("/").pop()?.replace(/-v\d+:\d+$/, "");
  return {
    slug: agent.id,
    name,
    role: model || "Custom agent",
    monogram,
    accent: palette.accent,
    tint: palette.tint,
  };
}
