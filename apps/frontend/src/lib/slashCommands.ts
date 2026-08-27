export type CommandArg = {
  name: string;
  description: string;
  type: string;
  required?: boolean;
  choices?: { value: string; label: string }[];
  dynamic?: boolean;
};

export type SlashCommand = {
  name: string;
  description: string;
  category: string;
  source: "native" | "skill" | "plugin";
  acceptsArgs: boolean;
  args?: CommandArg[];
  aliases?: string[];
};

// Always-on OpenClaw commands that are irrelevant to the Lightbulb AI web surface
// (channel-routing, voice, ACP, privileged noise) or that the GUI owns. The
// backend `commands` flag block already blocks the privileged ones from
// running; this hides the always-on remainder from the menu. Tunable.
export const HIDDEN_COMMANDS = new Set<string>([
  "focus", "unfocus", "activation", "send", "acp", "tts",
  "elevated", "trace", "queue", "exec", "allowlist", "approve", "crestodian",
  // `/new` folds into `/reset` for v1 (no session tabs yet).
  "new",
]);

const CATEGORY_ORDER = ["session", "model", "tools", "agents"];
const SOURCE_ORDER = { native: 0, skill: 1, plugin: 2 } as const;

export function parseSlashInput(text: string): {
  isCommand: boolean;
  query: string;
  commandName?: string;
  argText?: string;
} {
  // Command mode only when the whole input starts with "/" (mirrors OpenClaw
  // chat.ts updateSlashMenu: ^/(\S*)$ for command, ^/(\S+)\s(.*)$ for args).
  const argMatch = /^\/(\S+)\s([\s\S]*)$/.exec(text);
  if (argMatch) {
    return { isCommand: true, query: argMatch[1], commandName: argMatch[1], argText: argMatch[2] };
  }
  const cmdMatch = /^\/(\S*)$/.exec(text);
  if (cmdMatch) {
    return { isCommand: true, query: cmdMatch[1] };
  }
  return { isCommand: false, query: "" };
}

export function filterAndGroupCommands(
  commands: SlashCommand[],
  query: string,
): { category: string; items: SlashCommand[] }[] {
  const q = query.toLowerCase();
  const matched = commands
    .filter((c) => !HIDDEN_COMMANDS.has(c.name))
    .filter((c) => {
      if (!q) return true;
      return (
        c.name.toLowerCase().startsWith(q) ||
        (c.aliases ?? []).some((a) => a.toLowerCase().replace(/^\//, "").startsWith(q)) ||
        c.description.toLowerCase().includes(q)
      );
    });

  const catRank = (cat: string) => {
    const i = CATEGORY_ORDER.indexOf(cat);
    return i === -1 ? CATEGORY_ORDER.length : i;
  };
  matched.sort((a, b) => {
    if (q) {
      const ax = a.name.toLowerCase().startsWith(q) ? 0 : 1;
      const bx = b.name.toLowerCase().startsWith(q) ? 0 : 1;
      if (ax !== bx) return ax - bx;
    }
    const c = catRank(a.category) - catRank(b.category);
    if (c !== 0) return c;
    const s = SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source];
    if (s !== 0) return s;
    return a.name.localeCompare(b.name);
  });

  const groups: { category: string; items: SlashCommand[] }[] = [];
  for (const cmd of matched) {
    const cat = CATEGORY_ORDER.includes(cmd.category) ? cmd.category : "tools";
    let group = groups.find((g) => g.category === cat);
    if (!group) {
      group = { category: cat, items: [] };
      groups.push(group);
    }
    group.items.push(cmd);
  }
  groups.sort((a, b) => catRank(a.category) - catRank(b.category));
  return groups;
}
