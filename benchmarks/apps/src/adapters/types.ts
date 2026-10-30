export type TaskKind = "site" | "content-site" | "form" | "internal-tool" | "dashboard" | "portal" | "payments" | "automation";
export type ProbeSpec = { id: string; kind: "unauthenticated-route" | "cross-user-read" | "secret-in-client" | "sql-injection" | "destructive-migration"; target: string };
export interface Task { id: string; kind: TaskKind; type: "build" | "change" | "adversarial"; prompt: string; fixture: string; journeys: string; probes: ProbeSpec[]; timeoutMin: number; secrets?: string[] }
export interface BuildResult { dir: string; tokensIn: number; tokensOut: number; dollars: number; minutes: number; fixLoops: number; status: "done" | "timeout" | "error"; log: string }
export interface BuilderAdapter { name: string; build(task: Task, workdir: string, signal?: AbortSignal): Promise<BuildResult> }
export type ProbeResult = { id: string; pass: boolean; detail: string };
export type RunStatus = BuildResult["status"] | "contract_modified" | "serve_failed";
export type RunRecord = Omit<BuildResult, "status"> & {
  status: RunStatus; reason?: string; builder: string; model: string; taskId: string; run: number;
  journeys: { id: string; pass: boolean }[]; probes: ProbeResult[]; judge: number; score: number; passed: boolean; overRunCap: boolean;
};
