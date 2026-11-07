type Ev = { type?: string; total_cost_usd?: number; usage?: Record<string, number>; message?: { content?: any[] } };
const events = (s: string): Ev[] => s.split("\n").flatMap((l) => { try { return [JSON.parse(l) as Ev]; } catch { return []; } });

/** Tokens and dollars from the stream-json `result` event; null if the stream never produced one. */
export function parseResult(stream: string): { tokensIn: number; tokensOut: number; dollars: number } | null {
  const r = events(stream).reverse().find((e) => e.type === "result");
  if (!r) return null;
  const u = r.usage ?? {};
  return {
    tokensIn: (u.input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0),
    tokensOut: u.output_tokens ?? 0,
    dollars: r.total_cost_usd ?? 0,
  };
}

// Approximation: a fix loop is an Edit/Write to an already-edited file after any tool result mentioning "error".
export function countFixLoops(stream: string): number {
  const edited = new Set<string>();
  let errorSeen = false, loops = 0;
  for (const e of events(stream)) {
    for (const c of e.message?.content ?? []) {
      if (c.type === "tool_result" && /error/i.test(typeof c.content === "string" ? c.content : JSON.stringify(c.content))) errorSeen = true;
      if (c.type === "tool_use" && /^(Edit|Write|MultiEdit)$/.test(c.name) && c.input?.file_path) {
        if (errorSeen && edited.has(c.input.file_path)) { loops++; errorSeen = false; }
        edited.add(c.input.file_path);
      }
    }
  }
  return loops;
}

/** Codex `--json`: tokens summed over every `turn.completed` (a run has many turns). Null if none arrived. */
export function parseCodexUsage(stream: string): { tokensIn: number; tokensOut: number } | null {
  const turns = events(stream).filter((e: any) => e.type === "turn.completed");
  if (!turns.length) return null;
  return {
    tokensIn: turns.reduce((a, e: any) => a + (e.usage?.input_tokens ?? 0), 0),
    tokensOut: turns.reduce((a, e: any) => a + (e.usage?.output_tokens ?? 0) + (e.usage?.reasoning_output_tokens ?? 0), 0),
  };
}

// Approximation: a fix loop is a file_change touching an already-changed file after any failed command_execution.
export function countCodexFixLoops(stream: string): number {
  // codex mostly edits through the shell, so a loop is also: a command fails, then the same command later succeeds
  const changed = new Set<string>(), failedCmds = new Set<string>();
  let failed = false, loops = 0;
  for (const e of events(stream) as any[]) {
    const it = e.type === "item.completed" ? e.item : null;
    if (it?.type === "command_execution") {
      const cmd = String(it.command ?? "").trim();
      if (it.status === "failed" || (it.exit_code ?? 0) !== 0) { failed = true; failedCmds.add(cmd); }
      else if (failedCmds.delete(cmd)) { loops++; failed = false; continue; }
    }
    if (it?.type === "file_change") {
      const paths: string[] = (it.changes ?? []).map((c: any) => c.path);
      if (failed && paths.some((p) => changed.has(p))) { loops++; failed = false; }
      paths.forEach((p) => changed.add(p));
    }
  }
  return loops;
}
