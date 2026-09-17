export interface AgentIdentity {
  name?: string;
  theme?: string;
  emoji?: string;
  avatar?: string;
}

export interface AgentEntry {
  id: string;
  name?: string;
  identity?: AgentIdentity;
  model?: string;
  /** Paperclip roster fields. On AgentCore an `agents.list` row IS the Paperclip row, and it is
   *  the only identity source there — `agent.identity.get` has no Hermes implementation and
   *  `config.agents.list` does not exist (M6 task 10-C, scan rows 5/8). */
  role?: string;
  title?: string;
  capabilities?: string;
  icon?: string;
}

export interface ModelCatalogEntry {
  alias?: string;
}

export interface ConfigSnapshot {
  path: string;
  exists: boolean;
  raw: string | null;
  config: ConfigInner;
  hash?: string;
  valid: boolean;
}

export interface AgentConfigEntry {
  id: string;
  model?: string | { primary?: string; fallbacks?: string[] };
  tools?: { profile?: string; alsoAllow?: string[]; deny?: string[] };
  [key: string]: unknown;
}

/**
 * A runtime config document. TWO runtimes write one, and the two share no keys (M6 task 10-C,
 * scan row 5):
 *
 *  - ECS: `openclaw.json` — `agents.defaults`, `agents.list`, `tools`, `models.providers`.
 *  - AgentCore: a Hermes profile's `config.yaml` — `model`, `agent`, `toolsets`, `auxiliary`,
 *    `memory`, `skills`, `sessions`, `mcp_servers`, `plugins`. There is no `agents` key at all,
 *    and `config.get` without an `agentId` returns a MAP of these keyed by agent id.
 *
 * Both are declared so a surface reading the wrong runtime's keys is a type error rather than a
 * silent `undefined`. Neither is a contract the client can trust structurally — both are runtime
 * data, so every walker over them stays defensive.
 */
export interface ConfigInner {
  agents?: {
    defaults?: {
      models?: Record<string, ModelCatalogEntry>;
      model?: string | { primary?: string };
    };
    list?: AgentConfigEntry[];
  };
  tools?: { profile?: string; allow?: string[] };
  // Hermes (AgentCore) keys.
  model?: { default?: string; provider?: string; context_length?: number };
  agent?: { disabled_toolsets?: string[]; run_budget_seconds?: number; stall_guards?: boolean };
  toolsets?: string[];
  auxiliary?: Record<string, unknown>;
  memory?: Record<string, unknown>;
  skills?: Record<string, unknown>;
  sessions?: Record<string, unknown>;
  mcp_servers?: Record<string, unknown>;
  plugins?: { enabled?: string[]; entries?: Record<string, unknown> };
  [key: string]: unknown;
}

export interface AgentsListResponse {
  defaultId?: string;
  mainKey?: string;
  scope?: string;
  agents?: AgentEntry[];
}

export interface AgentFileEntry {
  name: string;
  path: string;
  missing: boolean;
  size?: number;
  updatedAtMs?: number;
}

export interface AgentFilesResponse {
  agentId: string;
  workspace: string;
  files: AgentFileEntry[];
}

export interface AgentFileContent {
  agentId: string;
  file: AgentFileEntry & { content?: string };
}

export interface ToolEntry {
  name: string;
  id?: string;
  label?: string;
  description?: string;
  profile?: string;
  category?: string;
  source?: "core" | "plugin";
  pluginId?: string;
  optional?: boolean;
  defaultProfiles?: string[];
  [key: string]: unknown;
}

export interface ToolCatalogProfile { id: string; label: string }
export interface ToolCatalogGroup { id: string; label: string; source?: "core" | "plugin"; tools: ToolEntry[] }

export interface ToolsCatalogResponse {
  agentId?: string;
  profiles?: ToolCatalogProfile[] | Record<string, unknown>;
  groups?: ToolCatalogGroup[];
  tools?: ToolEntry[];
  [key: string]: unknown;
}
