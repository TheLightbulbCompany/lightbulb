// apps/frontend/src/components/control/panels/skill-proposals-types.ts
// Mirrors openclaw src/skills/workshop/types.ts.

export type SkillProposalKind = "create" | "update";
export type SkillProposalStatus = "pending" | "applied" | "rejected" | "quarantined" | "stale";
export type SkillProposalScannerState = "pending" | "clean" | "failed" | "quarantined";

export interface SkillProposalManifestEntry {
  id: string;
  kind: SkillProposalKind;
  status: SkillProposalStatus;
  title: string;
  description: string;
  skillName: string;
  skillKey: string;
  createdAt: string;
  updatedAt: string;
  scanState: SkillProposalScannerState;
}

export interface SkillProposalManifest {
  updatedAt: string;
  proposals: SkillProposalManifestEntry[];
}

export interface SkillProposalRecord {
  id: string;
  kind: SkillProposalKind;
  status: SkillProposalStatus;
  title: string;
  description: string;
  createdAt: string;
  updatedAt: string;
  proposedVersion: string;
  goal?: string;
  evidence?: string;
  statusReason?: string;
}

export interface SkillProposalReadResult {
  record: SkillProposalRecord;
  content: string;
  supportFiles?: { path: string; content: string }[];
}

export interface SkillProposalApplyResult {
  record: SkillProposalRecord;
  targetSkillFile: string;
}
