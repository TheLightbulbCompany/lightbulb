import { describe, it, expect } from "vitest";
import { parseSlashInput, filterAndGroupCommands, type SlashCommand } from "@/lib/slashCommands";

const CMDS: SlashCommand[] = [
  { name: "help", description: "List commands", category: "tools", source: "native", acceptsArgs: false },
  { name: "model", description: "Switch model", category: "model", source: "native", acceptsArgs: true,
    args: [{ name: "model", description: "id", type: "string", choices: [{ value: "opus", label: "Opus" }] }] },
  { name: "reset", description: "Reset session", category: "session", source: "native", acceptsArgs: true },
  { name: "deep-research", description: "Research report", category: "tools", source: "skill", acceptsArgs: true },
];

describe("parseSlashInput", () => {
  it("detects a bare slash as command mode with empty query", () => {
    expect(parseSlashInput("/")).toEqual({ isCommand: true, query: "" });
  });
  it("detects a partial command name", () => {
    expect(parseSlashInput("/mo")).toMatchObject({ isCommand: true, query: "mo" });
  });
  it("enters arg mode once a space follows the command", () => {
    expect(parseSlashInput("/model op")).toMatchObject({ isCommand: true, commandName: "model", argText: "op" });
  });
  it("is NOT command mode for normal text", () => {
    expect(parseSlashInput("hello").isCommand).toBe(false);
  });
  it("is NOT command mode for a slash mid-message", () => {
    expect(parseSlashInput("a/b").isCommand).toBe(false);
  });
});

describe("filterAndGroupCommands", () => {
  it("groups by category, ordered session→model→tools", () => {
    const groups = filterAndGroupCommands(CMDS, "");
    expect(groups.map((g) => g.category)).toEqual(["session", "model", "tools"]);
  });
  it("prefix-filters by name and floats exact-prefix first", () => {
    const groups = filterAndGroupCommands(CMDS, "mo");
    const flat = groups.flatMap((g) => g.items.map((i) => i.name));
    expect(flat).toEqual(["model"]);
  });
  it("matches description substrings too", () => {
    const flat = filterAndGroupCommands(CMDS, "research").flatMap((g) => g.items.map((i) => i.name));
    expect(flat).toContain("deep-research");
  });
});
