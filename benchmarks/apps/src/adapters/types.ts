export type TaskKind = "site" | "content-site" | "form" | "internal-tool" | "dashboard" | "portal" | "payments" | "automation";
export type ProbeSpec = {
  id: string; kind: "unauthenticated-route" | "cross-user-read" | "secret-in-client" | "sql-injection" | "destructive-migration"; target: string;
  /** cross-user-read: JSON body user A POSTs to `target` to create a record (response must carry `id`). */
  create?: unknown;
  /** destructive-migration: SQL run on the fixture's schema before the build; every EVALMARK_* string in it must survive. */
  seed?: string;
  /** sql-injection: sign-up name for the probe's user (becomes the email's local-part prefix, e.g. "editor+probe" for a role the app grants by email). */
  as?: string;
};
export interface Task { id: string; kind: TaskKind; type: "build" | "change" | "adversarial"; prompt: string; fixture: string; journeys: string; probes: ProbeSpec[]; timeoutMin: number; secrets?: string[] }
export type Billing = "api" | "subscription";
export interface BuildResult { dir: string; tokensIn: number; tokensOut: number; dollars: number; minutes: number; fixLoops: number; costUnknown?: boolean; billing?: Billing; status: "done" | "timeout" | "error"; log: string }
export interface BuilderAdapter { name: string; billing?: Billing; defaultModel?: string; build(task: Task, workdir: string, signal?: AbortSignal): Promise<BuildResult> }
export type ProbeResult = { id: string; pass: boolean; detail: string };
export type RunStatus = BuildResult["status"] | "contract_modified" | "serve_failed";
export type RunRecord = Omit<BuildResult, "status"> & {
  status: RunStatus; reason?: string; builder: string; model: string; billing: Billing; taskId: string; run: number;
  journeys: { id: string; pass: boolean; error?: string }[]; probes: ProbeResult[]; judge: number; judgeReason?: string; judgeFailed?: boolean; score: number; passed: boolean; overRunCap: boolean; minutesToRunnable: number | null;
};
