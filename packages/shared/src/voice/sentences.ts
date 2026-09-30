// sentences.ts
/** Split off every completed sentence (terminator followed by whitespace). */
export function takeSentences(text: string): { ready: string; rest: string } {
  const match = /^[\s\S]*[.!?](?=\s)/.exec(text);
  if (!match) return { ready: "", rest: text };
  return { ready: match[0].trim(), rest: text.slice(match[0].length) };
}
