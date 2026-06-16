// Types mirroring the OpenClaw gateway's doctor.memory.* RPC payloads.
// Source: openclaw doctor.ts:84-135 (DoctorMemoryStatusPayload /
// DoctorMemoryDreamingPayload) and ui/controllers/dreaming.ts:56-190.
// All fields optional/defensive — a container with no memory manager
// returns only { agentId, embedding } with no `dreaming` block.

export type DreamPhaseKey = "light" | "deep" | "rem";

export interface DreamPhaseStatus {
  enabled?: boolean;
  cronStatus?: string;
  nextRunAt?: string;
}

export interface DreamingEntry {
  id?: string;
  summary?: string;
  recallCount?: number;
  createdAt?: string;
}

export interface DreamingStatus {
  enabled?: boolean;
  timezone?: string;
  verboseLogging?: boolean;
  storageMode?: "inline" | "separate" | "both";
  separateReports?: boolean;
  shortTermCount?: number;
  recallSignalCount?: number;
  dailySignalCount?: number;
  groundedSignalCount?: number;
  totalSignalCount?: number;
  phaseSignalCount?: number;
  lightPhaseHitCount?: number;
  remPhaseHitCount?: number;
  promotedTotal?: number;
  promotedToday?: number;
  storePath?: string;
  storeError?: string;
  lastPromotedAt?: string;
  shortTermEntries?: DreamingEntry[];
  signalEntries?: DreamingEntry[];
  promotedEntries?: DreamingEntry[];
  phases?: Partial<Record<DreamPhaseKey, DreamPhaseStatus>>;
}

export interface MemoryEmbeddingStatus {
  ok: boolean;
  error?: string;
}

export interface MemoryStatusResponse {
  agentId: string;
  provider?: string;
  embedding: MemoryEmbeddingStatus;
  dreaming?: DreamingStatus;
}

export interface DreamDiaryResponse {
  found: boolean;
  path?: string;
  content?: string;
  updatedAtMs?: number;
}

// Union of the doctor.memory.* write-action result payloads — every field is
// optional; the action sets only the ones relevant to it.
export interface DreamActionResponse {
  written?: number;
  removedEntries?: number;
  dedupedEntries?: number;
  keptEntries?: number;
  removedShortTermEntries?: number;
  archiveDir?: string;
}

export interface DreamAgentOption {
  agent_id: string;
  name?: string;
}
