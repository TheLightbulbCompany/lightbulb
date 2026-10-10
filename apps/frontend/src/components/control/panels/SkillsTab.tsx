"use client";

import Link from "next/link";
import { useState } from "react";

import { useStatusLoaded } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import {
  Loader2,
  RefreshCw,
  Search,
  AlertTriangle,
} from "lucide-react";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { SKILL_INTEGRATIONS } from "./skill-integrations";

// --- Types matching the skills.status response ---
// Hermes has no per-skill dependency manifest: the bridge answers every row with
// empty `missing`/`install`, so a skill is simply on or off.

interface SkillStatusEntry {
  name: string;
  description: string;
  source: string;
  skillKey: string;
  emoji?: string;
  primaryEnv?: string;
  bundled?: boolean;
  always: boolean;
  disabled: boolean;
  blockedByAllowlist: boolean;
  eligible: boolean;
  [key: string]: unknown;
}

interface SkillStatusReport {
  workspaceDir?: string;
  managedSkillsDir?: string;
  skills: SkillStatusEntry[];
  /** AgentCore only: the profile's STORED `skills.disabled`. See `toggleSkill`. */
  disabledNames?: string[];
}

// --- Category mapping ---

type Category = "All" | "Installed" | "Communication" | "Productivity" | "Research" | "Developer";

const CATEGORIES: Category[] = ["All", "Installed", "Communication", "Productivity", "Research", "Developer"];

const SKILL_CATEGORY_MAP: Record<string, Category> = {
  // Communication
  message: "Communication",
  email: "Communication",
  slack: "Communication",
  discord: "Communication",
  telegram: "Communication",
  whatsapp: "Communication",
  notify: "Communication",
  chat: "Communication",
  sms: "Communication",
  // Productivity
  calendar: "Productivity",
  schedule: "Productivity",
  task: "Productivity",
  todo: "Productivity",
  notes: "Productivity",
  memory: "Productivity",
  file: "Productivity",
  spreadsheet: "Productivity",
  google: "Productivity",
  notion: "Productivity",
  // Research
  search: "Research",
  browse: "Research",
  web: "Research",
  perplexity: "Research",
  firecrawl: "Research",
  scrape: "Research",
  crawl: "Research",
  wikipedia: "Research",
  arxiv: "Research",
  news: "Research",
};

function getSkillCategory(skill: SkillStatusEntry): Category {
  const nameLower = skill.name.toLowerCase();
  for (const [keyword, category] of Object.entries(SKILL_CATEGORY_MAP)) {
    if (nameLower.includes(keyword)) return category;
  }
  const descLower = (skill.description || "").toLowerCase();
  for (const [keyword, category] of Object.entries(SKILL_CATEGORY_MAP)) {
    if (descLower.includes(keyword)) return category;
  }
  return "Developer";
}

// --- Friendly descriptions for known skills ---

const FRIENDLY_DESCRIPTIONS: Record<string, string> = {
  perplexity: "Search the web with AI-powered answers and citations",
  firecrawl: "Scrape and crawl websites to extract structured data",
  elevenlabs: "Generate natural-sounding speech from text",
  memory: "Remember facts and context across conversations",
  computercontroller: "Control your computer with mouse and keyboard",
  developer: "Write, run, and debug code in your workspace",
  codesearch: "Search and navigate your codebase intelligently",
  webpilot: "Browse and interact with web pages",
  calendar: "Manage events and schedules",
};

function getFriendlyDescription(skill: SkillStatusEntry): string {
  const key = skill.name.toLowerCase().replace(/[^a-z0-9]/g, "");
  return FRIENDLY_DESCRIPTIONS[key] || skill.description || "Extend your agent's capabilities";
}

// Pull a human-readable message out of an RPC error. Gateway RPC rejections
// arrive as an Error whose message may be a JSON-encoded error object; surface
// its `message` field when present so the user sees the real reason instead of
// a swallowed console log.
function actionErrorText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.message === "string" && parsed.message) return parsed.message;
  } catch {
    /* not JSON — use raw */
  }
  return raw || "Please try again.";
}

// --- Create-custom-skill helper ---

const CREATE_SKILL_PROMPT =
  "Use your skill-creator skill to create a new custom skill for me. " +
  "It should: <describe what the skill does and when it should trigger>. " +
  "Put it in my workspace skills folder so it loads automatically.";

type SkillState = "installed" | "available";

function getSkillState(skill: SkillStatusEntry): SkillState {
  return skill.disabled ? "available" : "installed";
}

// --- Skills Tab ---

export function SkillsTab({ agentId }: { agentId?: string }) {
  // `undefined` is "not known for this workspace yet", never "this is ECS": firing
  // on a guess costs a microVM start on AgentCore, so the RPC waits for a definite
  // answer (same hold as AgentToolsTab).
  const statusLoaded = useStatusLoaded();
  const params = agentId ? { agentId } : {};
  const { data: raw, error, isLoading, mutate } = useRuntimeRpc<SkillStatusReport | SkillStatusEntry[]>(
    statusLoaded ? "skills.status" : null,
    params,
  );
  const callRpc = useRuntimeRpcMutation();
  // Turning a skill on or off writes the profile config, which the backend lets only admins do.
  const canManage = useIsWorkspaceAdmin();
  const [filter, setFilter] = useState("");
  const [activeCategory, setActiveCategory] = useState<Category>("All");
  const [showCreate, setShowCreate] = useState(false);
  const [copied, setCopied] = useState(false);

  // Normalize response: could be SkillStatusReport or raw array
  const skills: SkillStatusEntry[] = Array.isArray(raw) ? raw : raw?.skills ?? [];

  // Apply search filter
  const searchFiltered = filter
    ? skills.filter(
        (s) =>
          s.name.toLowerCase().includes(filter.toLowerCase()) ||
          s.description?.toLowerCase().includes(filter.toLowerCase()),
      )
    : skills;

  // Apply category filter
  const filtered = searchFiltered.filter((s) => {
    if (activeCategory === "All") return true;
    if (activeCategory === "Installed") return getSkillState(s) === "installed";
    return getSkillCategory(s) === activeCategory;
  });

  const installedSkills = filtered.filter((s) => getSkillState(s) === "installed");
  const availableSkills = filtered.filter((s) => getSkillState(s) === "available");

  // On Hermes a skill's enabled state is `skills.disabled` in the profile's
  // config.yaml, and the ONLY durable writer of a profile is the backend — the
  // bridge's own write would never reach S3, so the backend refuses `skills.update`. The
  // roster-wide `config.patch` (no agentId) is the door, and because a deep merge
  // replaces a list wholesale the patch has to carry every name that stays off.
  //
  // That list is `disabledNames` — the STORED names, unioned across the roster, served by
  // the BACKEND (`runtime_rpc.agentcore_disabled_skill_names`) because only it holds every
  // profile's config.yaml. Never "which rendered rows are off" (reviews I3/N2): a disabled
  // skill that is not on the profile the runtime scanned renders in no row at all, and a
  // profile written by `agents.create` carries no `disabled` key; either omission would ride
  // this wholesale list replace into a silent RE-ENABLE of a skill somebody deliberately
  // turned off.
  //
  // No fallback to the rows (review N3): that IS the truncating path, so guessing with it
  // would write the wrong list on the one image where it could fire. Refusing is safer —
  // and no shippable image can reach it, since every older bridge stubs `skills.status`
  // and the panel renders an error with no toggle at all.
  const storedDisabled = Array.isArray(raw) ? undefined : raw?.disabledNames;
  const toggleSkill = async (skill: SkillStatusEntry) => {
    if (!storedDisabled) {
      throw new Error(
        "This runtime did not report which skills are off, so changing one could silently " +
          "re-enable another. Reload, and update the agent runtime if it persists.",
      );
    }
    const disabled = new Set(storedDisabled);
    if (skill.disabled) disabled.delete(skill.name);
    else disabled.add(skill.name);
    await callRpc("config.patch", {
      patch: { skills: { disabled: [...disabled].sort() } },
    });
  };

  if (isLoading || !statusLoaded) {
    return (
      <div className="flex-1 flex items-center justify-center py-20">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  return (
    <div className="bg-[var(--cream)] min-h-full">
      <div className="p-6 space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-editorial-serif text-title">
              Skill Store
            </h2>
            <p className="text-sm text-[var(--midnight-muted)] mt-0.5">
              Your agents&apos; skills. Turn one off, or ask an agent to write a new one.
            </p>
            {!canManage && (
              <p className="text-xs text-[var(--midnight-muted)] mt-1">Only admins can change skills.</p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setShowCreate((v) => !v);
                setCopied(false);
              }}
              aria-expanded={showCreate}
              className="text-xs border-[var(--edge)]"
            >
              + Create custom skill
            </Button>
            <Button variant="ghost" size="sm" onClick={() => mutate()} className="text-[var(--midnight-muted)] hover:text-[var(--midnight)]" aria-label="Refresh skills">
              <RefreshCw size={16} />
            </Button>
          </div>
        </div>

        {/* Create-custom-skill helper */}
        {showCreate && (
          <div className="rounded-xl border border-[var(--edge)] bg-cream p-4 space-y-3">
            <p className="text-sm text-[var(--midnight)]">
              Your agent writes custom skills itself — it has a built-in <strong>skill-creator</strong> skill.
              Describe what you want in chat; the new skill appears here once it&apos;s written.
            </p>
            <pre className="text-xs text-[var(--midnight-soft)] bg-[var(--cream)] border border-[var(--edge)] rounded-md p-3 whitespace-pre-wrap">
              {CREATE_SKILL_PROMPT}
            </pre>
            <Button
              variant="outline"
              size="sm"
              className="text-xs border-[var(--edge)]"
              onClick={() => {
                void navigator.clipboard?.writeText(CREATE_SKILL_PROMPT).then(() => setCopied(true));
              }}
            >
              {copied ? "Copied — paste it in chat" : "Copy prompt"}
            </Button>
          </div>
        )}

        {/* Search bar */}
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--midnight-muted)]" />
          <Input aria-label="Search skills"
            placeholder="Search skills..."
            className="pl-10 h-10 text-sm bg-cream-deep border-[var(--edge)] rounded-lg"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        {/* Category pills */}
        <div className="flex items-center gap-2 overflow-x-auto pb-1">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => setActiveCategory(cat)}
              className={cn(
                "px-3.5 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors border",
                activeCategory === cat
                  ? "bg-[var(--midnight)] text-cream border-[var(--midnight)]"
                  : "bg-cream text-[var(--midnight-soft)] border-[var(--edge)] hover:border-[var(--midnight-faint)]",
              )}
            >
              {cat}
              {cat === "Installed" && ` (${skills.filter((s) => getSkillState(s) === "installed").length})`}
            </button>
          ))}
        </div>

        {/* No results */}
        {filtered.length === 0 && (
          <p className="text-sm text-[var(--midnight-muted)] text-center py-8">
            No skills found.
          </p>
        )}

        {/* Installed skills section */}
        {installedSkills.length > 0 && activeCategory !== "Installed" && (
          <div className="space-y-3">
            <h3
              className="text-sm font-semibold text-[var(--midnight)] tracking-caps"
              style={{ fontFamily: "'Lora', serif" }}
            >
              Installed
            </h3>
            <div className="grid gap-3">
              {installedSkills.map((skill) => (
                <SkillCard
                  key={skill.skillKey || skill.name}
                  skill={skill}
                  onToggle={canManage ? toggleSkill : undefined}
                  onRefresh={mutate}
                  variant="installed"
                />
              ))}
            </div>
          </div>
        )}

        {/* When on Installed category, show all installed in one section */}
        {activeCategory === "Installed" && installedSkills.length > 0 && (
          <div className="grid gap-3">
            {installedSkills.map((skill) => (
              <SkillCard
                key={skill.skillKey || skill.name}
                skill={skill}
                onToggle={canManage ? toggleSkill : undefined}
                onRefresh={mutate}
                variant="installed"
              />
            ))}
          </div>
        )}

        {/* Available skills section */}
        {availableSkills.length > 0 && activeCategory !== "Installed" && (
          <div className="space-y-3">
            <h3
              className="text-sm font-semibold text-[var(--midnight)] tracking-caps"
              style={{ fontFamily: "'Lora', serif" }}
            >
              Available
            </h3>
            <div className="grid gap-3">
              {availableSkills.map((skill) => (
                <SkillCard
                  key={skill.skillKey || skill.name}
                  skill={skill}
                  onToggle={canManage ? toggleSkill : undefined}
                  onRefresh={mutate}
                  variant="available"
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Integration chip ---

/**
 * "works with X" chip for skills that pair with a curated integration.
 * Deep-links to the Connections panel where the account can be connected.
 */
function IntegrationChip({ skillName }: { skillName: string }) {
  const integration = SKILL_INTEGRATIONS[skillName];
  if (!integration) return null;
  return (
    <Link
      href="/chat?panel=connections"
      className="inline-flex items-center px-2 py-0.5 rounded-full text-2xs font-medium bg-cream-deep text-[var(--midnight-soft)] hover:text-[var(--midnight)] shrink-0"
      title="Connect this account under Connections"
    >
      works with {integration.label}
    </Link>
  );
}

// --- Skill Card ---

function SkillCard({
  skill,
  onRefresh,
  onToggle,
  variant,
}: {
  skill: SkillStatusEntry;
  onRefresh: () => void;
  // Enabling a skill is runtime-specific (see `toggleSkill`), so the card is
  // handed the operation rather than deciding which RPC to send.
  // Absent for a non-admin: the card shows no toggle.
  onToggle?: (skill: SkillStatusEntry) => Promise<void>;
  variant: "installed" | "available";
}) {
  const [toggleLoading, setToggleLoading] = useState(false);
  // Surfaced to the user when a toggle fails. Previously these errors
  // were only console.error'd, so a failed enable looked like a no-op
  // and the user couldn't tell why the skill didn't change.
  const [actionError, setActionError] = useState<string | null>(null);

  const handleToggle = async () => {
    if (!onToggle) return;
    setToggleLoading(true);
    setActionError(null);
    try {
      await onToggle(skill);
      capture("skill_toggled", { skill: skill.name, enabled: skill.disabled });
      onRefresh();
    } catch (err) {
      console.error("Failed to toggle skill:", err);
      setActionError(
        `Couldn't ${skill.disabled ? "enable" : "disable"} this skill: ${actionErrorText(err)}`,
      );
    } finally {
      setToggleLoading(false);
    }
  };

  const friendlyDesc = getFriendlyDescription(skill);

  if (variant === "installed") {
    return (
      <div className="rounded-xl border border-[var(--edge)] bg-cream p-4 space-y-3">
        {/* Top row: icon + info + badge + actions */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0 flex-1">
            <span className="text-2xl shrink-0 mt-0.5">{skill.emoji || "🧩"}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h3
                  className="text-sm font-semibold text-[var(--midnight)] truncate"
                >
                  {skill.name}
                </h3>
                <span className="border border-ok inline-flex items-center px-2 py-0.5 rounded-full text-2xs font-medium bg-cream text-ok shrink-0">
                  Installed
                </span>
                <IntegrationChip skillName={skill.name} />
              </div>
              <p
                className="text-xs text-[var(--midnight-muted)] mt-0.5 line-clamp-2"
              >
                {friendlyDesc}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {skill.always ? (
              // Managed/required skills have no toggle. Explain why, so it doesn't
              // read as "I can't modify this and don't know why".
              <span
                className="inline-flex items-center px-2.5 py-1 rounded-full text-2xs font-medium bg-cream-deep text-[var(--midnight-muted)] cursor-help"
                title="This skill is always on — it's managed by Lightbulb and can't be turned off."
              >
                Always on
              </span>
            ) : onToggle ? (
              <button
                onClick={handleToggle}
                disabled={toggleLoading}
                className="px-3 py-1.5 rounded-lg text-xs font-medium border border-[var(--edge)] text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:border-[var(--midnight-faint)] transition-colors"
              >
                {toggleLoading ? <Loader2 size={12} strokeWidth={1.75} className="animate-spin" /> : "Disable"}
              </button>
            ) : null}
          </div>
        </div>

        {/* Action error — surfaced instead of being swallowed */}
        {actionError && (
          <div className="flex items-start gap-1.5 text-xs text-err">
            <AlertTriangle size={12} strokeWidth={1.75} className="shrink-0 mt-0.5" />
            <span>{actionError}</span>
          </div>
        )}
      </div>
    );
  }

  // --- Available (disabled) skill card ---
  return (
    <div className="rounded-xl border border-[var(--edge)] bg-cream p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0 flex-1">
          <span className="text-2xl shrink-0 mt-0.5">{skill.emoji || "🧩"}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3
                className="text-sm font-semibold text-[var(--midnight)] truncate"
              >
                {skill.name}
              </h3>
              <IntegrationChip skillName={skill.name} />
            </div>
            <p
              className="text-xs text-[var(--midnight-muted)] mt-0.5 line-clamp-2"
            >
              {friendlyDesc}
            </p>

          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {/* Install / Enable button */}
          {onToggle && (
          <button
            onClick={handleToggle}
            disabled={toggleLoading || skill.always}
            className="px-4 py-1.5 rounded-full text-xs font-medium bg-[var(--midnight)] text-cream hover:opacity-90 transition-colors disabled:opacity-50"
          >
            {toggleLoading ? <Loader2 size={12} strokeWidth={1.75} className="animate-spin" /> : "Install"}
          </button>
          )}
        </div>
      </div>

      {/* Action error — surfaced instead of being swallowed */}
      {actionError && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-err">
          <AlertTriangle size={12} strokeWidth={1.75} className="shrink-0 mt-0.5" />
          <span>{actionError}</span>
        </div>
      )}
    </div>
  );
}
