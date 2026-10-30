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
