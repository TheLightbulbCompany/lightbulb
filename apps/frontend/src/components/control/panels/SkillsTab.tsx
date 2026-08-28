"use client";

import Link from "next/link";
import { useState } from "react";

import { capture } from "@/lib/analytics";
import {
  Loader2,
  RefreshCw,
  Search,
  AlertTriangle,
  Download,
  ChevronDown,
  Monitor,
} from "lucide-react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { SKILL_INTEGRATIONS } from "./skill-integrations";
import { ClawHubSearch } from "./ClawHubSearch";

// --- Types matching OpenClaw skills.status response ---

interface SkillInstallSpec {
  id: string;
  kind: string;
  label: string;
  bins: string[];
}

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
  requirements: { bins: string[]; env: string[]; config: string[]; os: string[] };
  missing: { bins: string[]; env: string[]; config: string[]; os: string[] };
  install: SkillInstallSpec[];
  [key: string]: unknown;
}

interface SkillStatusReport {
  workspaceDir?: string;
  managedSkillsDir?: string;
  skills: SkillStatusEntry[];
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

// --- Dependency viability check ---

type SkillState = "installed" | "available" | "unsupported";

function getSkillState(skill: SkillStatusEntry): SkillState {
  const hasMissingBins = (skill.missing?.bins?.length ?? 0) > 0;

  // No missing binaries: installed if enabled, available if disabled
  if (!hasMissingBins) return skill.disabled ? "available" : "installed";

  // Has missing bins — not functional regardless of enabled state
  // Check if there's a viable (non-brew) install path
  if (hasViableInstallSpec(skill)) return "available";
  return "unsupported";
}

function hasViableInstallSpec(skill: SkillStatusEntry): boolean {
  if ((skill.missing?.bins?.length ?? 0) === 0) return true;
  if (!skill.install || skill.install.length === 0) return false;

  const missingSet = new Set(skill.missing.bins);
  for (const spec of skill.install) {
    if (spec.kind === "brew") continue; // brew not available on Alpine containers
    if (spec.bins.some((b) => missingSet.has(b))) return true;
  }
  return false;
}

// --- Skills Tab ---

export function SkillsTab({ agentId }: { agentId?: string }) {
  const params = agentId ? { agentId } : {};
  const { data: raw, error, isLoading, mutate } = useGatewayRpc<SkillStatusReport | SkillStatusEntry[]>(
    "skills.status",
    params,
  );
  const callRpc = useGatewayRpcMutation();
  const [filter, setFilter] = useState("");
  const [activeCategory, setActiveCategory] = useState<Category>("All");
  const [unsupportedCollapsed, setUnsupportedCollapsed] = useState(true);
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
  const unsupportedSkills = filtered.filter((s) => getSkillState(s) === "unsupported");

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center py-20">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Retry
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
            <h2 className="text-xl font-semibold" style={{ fontFamily: "'Lora', serif" }}>
              Skill Store
            </h2>
            <p className="text-sm text-[var(--midnight-muted)] mt-0.5" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              Discover and install skills for your agent
            </p>
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
              style={{ fontFamily: "'DM Sans', sans-serif" }}
            >
              + Create custom skill
            </Button>
            <Button variant="ghost" size="sm" onClick={() => mutate()} className="text-[var(--midnight-muted)] hover:text-[var(--midnight)]">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {/* Create-custom-skill helper */}
        {showCreate && (
          <div className="rounded-xl border border-[var(--edge)] bg-white p-4 space-y-3">
            <p className="text-sm text-[var(--midnight)]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              Your agent writes custom skills itself — it has a built-in <strong>skill-creator</strong> skill.
              Describe what you want in chat; the new skill appears here once it&apos;s written.
            </p>
            <pre className="text-xs text-[#5a5549] bg-[var(--cream)] border border-[var(--edge)] rounded-md p-3 whitespace-pre-wrap">
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
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-[var(--midnight-muted)]" />
          <Input
            placeholder="Search skills..."
            className="pl-10 h-10 text-sm bg-white border-[var(--edge)] rounded-lg"
            style={{ fontFamily: "'DM Sans', sans-serif" }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        {/* ClawHub registry search — in-app browse + one-click install */}
        <ClawHubSearch query={filter} callRpc={callRpc} onInstalled={mutate} />

        {/* Category pills */}
        <div className="flex items-center gap-2 overflow-x-auto pb-1">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => setActiveCategory(cat)}
              className={cn(
                "px-3.5 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors border",
                activeCategory === cat
                  ? "bg-[var(--midnight)] text-white border-[var(--midnight)]"
                  : "bg-white text-[#5a5549] border-[var(--edge)] hover:border-[#c5c0b6]",
              )}
              style={{ fontFamily: "'DM Sans', sans-serif" }}
            >
              {cat}
              {cat === "Installed" && ` (${skills.filter((s) => getSkillState(s) === "installed").length})`}
            </button>
          ))}
        </div>

        {/* No results */}
        {filtered.length === 0 && (
          <p className="text-sm text-[var(--midnight-muted)] text-center py-8" style={{ fontFamily: "'DM Sans', sans-serif" }}>
            No skills found.
          </p>
        )}

        {/* Installed skills section */}
        {installedSkills.length > 0 && activeCategory !== "Installed" && (
          <div className="space-y-3">
            <h3
              className="text-sm font-semibold text-[var(--midnight)] tracking-wide"
              style={{ fontFamily: "'Lora', serif" }}
            >
              Installed
            </h3>
            <div className="grid gap-3">
              {installedSkills.map((skill) => (
                <SkillCard
                  key={skill.skillKey || skill.name}
                  skill={skill}
                  callRpc={callRpc}
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
                callRpc={callRpc}
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
              className="text-sm font-semibold text-[var(--midnight)] tracking-wide"
              style={{ fontFamily: "'Lora', serif" }}
            >
              Available
            </h3>
            <div className="grid gap-3">
              {availableSkills.map((skill) => (
                <SkillCard
                  key={skill.skillKey || skill.name}
                  skill={skill}
                  callRpc={callRpc}
                  onRefresh={mutate}
                  variant="available"
                />
              ))}
            </div>
          </div>
        )}

        {/* Not supported in cloud section */}
        {unsupportedSkills.length > 0 && activeCategory !== "Installed" && (
          <div className="space-y-3">
            <button
              onClick={() => setUnsupportedCollapsed(!unsupportedCollapsed)}
              className="flex items-center gap-2 text-sm font-semibold text-[#b5ae9e] hover:text-[var(--midnight-muted)] transition-colors w-full text-left"
              style={{ fontFamily: "'Lora', serif" }}
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  unsupportedCollapsed && "-rotate-90",
                )}
              />
              Not supported in cloud ({unsupportedSkills.length})
            </button>
            {!unsupportedCollapsed && (
              <>
                <p
                  className="text-xs text-[#b5ae9e] pl-5"
                  style={{ fontFamily: "'DM Sans', sans-serif" }}
                >
                  These skills require tools that aren&apos;t available in cloud
                  containers. They work when running OpenClaw locally on your Mac
                  or PC.
                </p>
                <div className="grid gap-2 pl-5">
                  {unsupportedSkills.map((skill) => (
                    <div
                      key={skill.skillKey || skill.name}
                      className="rounded-xl border border-[var(--edge)] bg-white/60 p-3 opacity-60"
                    >
                      <div className="flex items-center gap-3">
                        <span className="text-lg flex-shrink-0 grayscale">
                          {skill.emoji || "🧩"}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <h3
                              className="text-xs font-semibold text-[var(--midnight-muted)] truncate"
                              style={{ fontFamily: "'DM Sans', sans-serif" }}
                            >
                              {skill.name}
                            </h3>
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-[#f0ebe2] text-[var(--midnight-muted)] flex-shrink-0">
                              <Monitor className="h-2.5 w-2.5" />
                              Desktop Only
                            </span>
                          </div>
                          {(skill.missing?.bins?.length ?? 0) > 0 && (
                            <p className="text-[11px] text-[#b5ae9e] mt-0.5">
                              Requires: {skill.missing.bins.join(", ")}
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

      </div>
    </div>
  );
}

// --- Integration chip ---

/**
 * "works with X" chip for skills that pair with a curated integration.
 * Deep-links to the Integrations panel where the account can be connected.
 */
function IntegrationChip({ skillName }: { skillName: string }) {
  const integration = SKILL_INTEGRATIONS[skillName];
  if (!integration) return null;
  return (
    <Link
      href="/chat?panel=integrations"
      className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-[#f0ebe2] text-[#5a5549] hover:text-[var(--midnight)] flex-shrink-0"
      style={{ fontFamily: "'DM Sans', sans-serif" }}
      title="Connect this account under Integrations"
    >
      works with {integration.label}
    </Link>
  );
}

// --- Skill Card ---

function SkillCard({
  skill,
  callRpc,
  onRefresh,
  variant,
}: {
  skill: SkillStatusEntry;
  callRpc: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
  onRefresh: () => void;
  variant: "installed" | "available";
}) {
  const [toggleLoading, setToggleLoading] = useState(false);
  const [installLoading, setInstallLoading] = useState<string | null>(null);
  // Surfaced to the user when a toggle/install fails. Previously these errors
  // were only console.error'd, so a failed enable/install looked like a no-op
  // and the user couldn't tell why the skill didn't change.
  const [actionError, setActionError] = useState<string | null>(null);

  const hasMissing =
    (skill.missing?.bins?.length ?? 0) > 0 ||
    (skill.missing?.env?.length ?? 0) > 0 ||
    (skill.missing?.config?.length ?? 0) > 0;

  const handleToggle = async () => {
    setToggleLoading(true);
    setActionError(null);
    try {
      await callRpc("skills.update", {
        skillKey: skill.skillKey || skill.name,
        enabled: skill.disabled,
      });
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

  const handleInstall = async (spec: SkillInstallSpec) => {
    setInstallLoading(spec.id);
    setActionError(null);
    try {
      await callRpc("skills.install", {
        name: skill.name,
        installId: spec.id,
        timeoutMs: 120000,
      });
      capture("skill_installed", { skill: skill.name });
      onRefresh();
    } catch (err) {
      console.error("Failed to install:", err);
      setActionError(`Couldn't install this skill: ${actionErrorText(err)}`);
    } finally {
      setInstallLoading(null);
    }
  };

  const friendlyDesc = getFriendlyDescription(skill);

  if (variant === "installed") {
    return (
      <div className="rounded-xl border border-[var(--edge)] bg-white p-4 space-y-3">
        {/* Top row: icon + info + badge + actions */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0 flex-1">
            <span className="text-2xl flex-shrink-0 mt-0.5">{skill.emoji || "🧩"}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h3
                  className="text-sm font-semibold text-[var(--midnight)] truncate"
                  style={{ fontFamily: "'DM Sans', sans-serif" }}
                >
                  {skill.name}
                </h3>
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-[#e8f5e9] text-[#2d8a4e] flex-shrink-0">
                  Installed
                </span>
                <IntegrationChip skillName={skill.name} />
              </div>
              <p
                className="text-xs text-[var(--midnight-muted)] mt-0.5 line-clamp-2"
                style={{ fontFamily: "'DM Sans', sans-serif" }}
              >
                {friendlyDesc}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            {skill.always ? (
              // Managed/required skills have no toggle. Explain why, so it doesn't
              // read as "I can't modify this and don't know why".
              <span
                className="inline-flex items-center px-2.5 py-1 rounded-full text-[10px] font-medium bg-[#f0ebe2] text-[var(--midnight-muted)] cursor-help"
                title="This skill is always on — it's managed by Lightbulb AI and can't be turned off."
                style={{ fontFamily: "'DM Sans', sans-serif" }}
              >
                Always on
              </span>
            ) : (
              <button
                onClick={handleToggle}
                disabled={toggleLoading}
                className="px-3 py-1.5 rounded-lg text-xs font-medium border border-[var(--edge)] text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:border-[#c5c0b6] transition-colors"
                style={{ fontFamily: "'DM Sans', sans-serif" }}
              >
                {toggleLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : "Disable"}
              </button>
            )}
          </div>
        </div>

        {/* Action error — surfaced instead of being swallowed */}
        {actionError && (
          <div className="flex items-start gap-1.5 text-xs text-red-600">
            <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5" />
            <span>{actionError}</span>
          </div>
        )}

        {/* Missing dependencies */}
        {hasMissing && (
          <div className="space-y-1">
            {(skill.missing?.bins?.length ?? 0) > 0 && (
              <div className="flex items-center gap-1.5 text-xs text-orange-500">
                <AlertTriangle className="h-3 w-3 flex-shrink-0" />
                <span>Missing: {skill.missing.bins.join(", ")}</span>
              </div>
            )}
            {(skill.missing?.env?.length ?? 0) > 0 && (
              <div className="flex items-center gap-1.5 text-xs text-orange-500">
                <AlertTriangle className="h-3 w-3 flex-shrink-0" />
                <span>Missing env: {skill.missing.env.join(", ")}</span>
              </div>
            )}
            {(skill.missing?.config?.length ?? 0) > 0 && (
              <div className="flex items-center gap-1.5 text-xs text-orange-500">
                <AlertTriangle className="h-3 w-3 flex-shrink-0" />
                <span>Missing config: {skill.missing.config.join(", ")}</span>
              </div>
            )}
          </div>
        )}

        {/* Install buttons for missing bins */}
        {skill.install?.length > 0 && (skill.missing?.bins?.length ?? 0) > 0 && (
          <div className="flex flex-wrap gap-2">
            {skill.install
              .filter((spec) => spec.kind !== "brew")
              .map((spec) => (
                <Button
                  key={spec.id}
                  variant="outline"
                  size="sm"
                  className="text-xs gap-1.5 rounded-lg"
                  onClick={() => handleInstall(spec)}
                  disabled={installLoading !== null}
                >
                  {installLoading === spec.id ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Download className="h-3 w-3" />
                  )}
                  {spec.label || `Install via ${spec.kind}`}
                </Button>
              ))}
          </div>
        )}
      </div>
    );
  }

  // --- Available (disabled) skill card ---
  return (
    <div className="rounded-xl border border-[var(--edge)] bg-white p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0 flex-1">
          <span className="text-2xl flex-shrink-0 mt-0.5">{skill.emoji || "🧩"}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3
                className="text-sm font-semibold text-[var(--midnight)] truncate"
                style={{ fontFamily: "'DM Sans', sans-serif" }}
              >
                {skill.name}
              </h3>
              <IntegrationChip skillName={skill.name} />
            </div>
            <p
              className="text-xs text-[var(--midnight-muted)] mt-0.5 line-clamp-2"
              style={{ fontFamily: "'DM Sans', sans-serif" }}
            >
              {friendlyDesc}
            </p>

            {/* Missing dependencies inline */}
            {hasMissing && (
              <div className="mt-2 space-y-0.5">
                {(skill.missing?.bins?.length ?? 0) > 0 && (
                  <div className="flex items-center gap-1 text-[11px] text-orange-500">
                    <AlertTriangle className="h-2.5 w-2.5 flex-shrink-0" />
                    <span>Needs: {skill.missing.bins.join(", ")}</span>
                  </div>
                )}
                {(skill.missing?.env?.length ?? 0) > 0 && (
                  <div className="flex items-center gap-1 text-[11px] text-orange-500">
                    <AlertTriangle className="h-2.5 w-2.5 flex-shrink-0" />
                    <span>Needs env: {skill.missing.env.join(", ")}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Install dependency buttons */}
          {skill.install?.filter((s) => s.kind !== "brew").length > 0 && (skill.missing?.bins?.length ?? 0) > 0 && (
            <>
              {skill.install
                .filter((spec) => spec.kind !== "brew")
                .map((spec) => (
                  <button
                    key={spec.id}
                    onClick={() => handleInstall(spec)}
                    disabled={installLoading !== null}
                    className="px-3 py-1.5 rounded-full text-xs font-medium border border-[#ffe0b2] text-[#8a6a22] bg-[#fff8e1] hover:bg-[#fff3cc] transition-colors"
                    style={{ fontFamily: "'DM Sans', sans-serif" }}
                  >
                    {installLoading === spec.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <>{spec.label || `Install ${spec.kind}`}</>
                    )}
                  </button>
                ))}
            </>
          )}

          {/* Install / Enable button */}
          <button
            onClick={handleToggle}
            disabled={toggleLoading || skill.always}
            className="px-4 py-1.5 rounded-full text-xs font-medium bg-[#2d8a4e] text-white hover:bg-[#247a42] transition-colors disabled:opacity-50"
            style={{ fontFamily: "'DM Sans', sans-serif" }}
          >
            {toggleLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : "Install"}
          </button>
        </div>
      </div>

      {/* Action error — surfaced instead of being swallowed */}
      {actionError && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-red-600">
          <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5" />
          <span>{actionError}</span>
        </div>
      )}
    </div>
  );
}
