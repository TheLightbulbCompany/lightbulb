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

// Agents are NOT colour-coded. §06: a state colour doubling as decoration
// "makes every real alert weaker" — an avatar in --ok or --err reads as a
// status. §07 ("Not yet drawn") is explicit that until real agent icons
// exist, agents are told apart by name/initials in type, not decoration. So
// every slot is the SAME neutral chip; identity comes from the monogram the
// caller already renders. Uses the shadcn contract tokens (not the raw
// --midnight/--cream-deep/--edge) so a consumer rendered under `.dark`
// (admin) still resolves fg-on-bg correctly without this file knowing about
// grounds. The length is arbitrary now that every entry is identical:
// paletteFor's `hash % PALETTE.length` returns the same chip for any slug.
export const PALETTE: ReadonlyArray<{ accent: string; tint: string }> = Array(6).fill({
  accent: "var(--foreground)",
  tint: "var(--accent)",
});

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
  const vibe = firstSentence(stripMarkdown(listing.description_md)) || "A Lightbulb AI marketplace agent.";
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
