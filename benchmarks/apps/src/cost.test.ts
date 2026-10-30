import { describe, expect, it } from "vitest";
import { countFixLoops, parseResult } from "./cost";

const line = (o: unknown) => JSON.stringify(o);
const tu = (name: string, file: string) => line({ type: "assistant", message: { content: [{ type: "tool_use", id: "x", name, input: { file_path: file } }] } });
const tr = (text: string) => line({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: "x", content: text }] } });

describe("cost", () => {
  it("reads tokens and dollars from the result event", () => {
    const s = [line({ type: "system" }), line({ type: "result", subtype: "success", total_cost_usd: 1.25, usage: { input_tokens: 100, cache_creation_input_tokens: 50, cache_read_input_tokens: 850, output_tokens: 300 } })].join("\n");
    expect(parseResult(s)).toEqual({ tokensIn: 1000, tokensOut: 300, dollars: 1.25 });
  });
  it("returns null with no result event", () => {
    expect(parseResult("not json\n" + line({ type: "system" }))).toBeNull();
  });
  it("counts an edit after an error to an already-edited file as one fix loop", () => {
    const s = [tu("Write", "a.ts"), tr("Type error: x"), tu("Edit", "a.ts"), tr("ok"), tu("Edit", "a.ts"), tu("Write", "b.ts")].join("\n");
    expect(countFixLoops(s)).toBe(1);
  });
});
