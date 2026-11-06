/**
 * Paperclip-relevant agent metadata. Mirrors the backend's
 * core/services/paperclip_meta.py shape — both write into
 * agent_entry.params.paperclip.
 *
 * Kept in sync MANUALLY with Paperclip's
 * paperclip/packages/shared/src/constants.ts AGENT_ROLES. If the
 * backend's PAPERCLIP_AGENT_ROLES test fails, this file needs the
 * same update.
 */

export const PAPERCLIP_AGENT_ROLES = [
  "ceo",
  "cto",
  "cmo",
  "cfo",
  "security",
  "engineer",
  "designer",
  "pm",
  "qa",
  "devops",
  "researcher",
  "general",
] as const;

export type PaperclipRole = (typeof PAPERCLIP_AGENT_ROLES)[number];

export const PAPERCLIP_ROLE_LABELS: Record<PaperclipRole, string> = {
  ceo: "Coordinator",
  cto: "CTO",
  cmo: "CMO",
  cfo: "CFO",
  security: "Security",
  engineer: "Engineer",
  designer: "Designer",
  pm: "Product manager",
  qa: "QA",
  devops: "DevOps",
  researcher: "Researcher",
  general: "General (no specific role)",
};

export interface PaperclipMeta {
  role: PaperclipRole;
  title?: string;
  capabilities?: string;
  icon?: string;
}
