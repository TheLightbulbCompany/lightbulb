// The search API marks matches with \u0001…\u0002 instead of HTML, so a
// message's own text can never become markup here.
export function snippetParts(snippet: string): { text: string; match: boolean }[] {
  return snippet.split(/(\u0001[^\u0002]*\u0002)/).filter(Boolean).map((part) => part.startsWith("\u0001")
    ? { text: part.slice(1, -1), match: true }
    : { text: part, match: false });
}

export const plainSnippet = (snippet: string) => snippet.replace(/[\u0001\u0002]/g, "");

export function SearchSnippet({ snippet }: { snippet: string }) {
  return <>{snippetParts(snippet).map((part, index) => part.match ? <mark key={index}>{part.text}</mark> : part.text)}</>;
}
