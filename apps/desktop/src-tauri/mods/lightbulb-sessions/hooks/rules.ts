// Who sent a Lightbulb delivery, read from the header the desktop app writes
// (local_sessions.rs: OWNER_LABEL, CLAUDE_OWNER_NOTE, HEADER_PREFIX, Command::header). Teammate lines
// that could pass for a header are quoted by the app, so only a match at the very start counts.
export type Sender =
  | { kind: 'owner' }
  | { kind: 'person'; name: string; id?: string }
  | { kind: 'agent'; name: string; id?: string }
  | { kind: 'session'; name: string; id?: string }
  | { kind: 'peer' }

const OWNER = /^\[Sent from Lightbulb by you\]\n\[Lightbulb verified sender:/
const TEAMMATE = /^\[Lightbulb teammate contribution(?: from (?:@\S+ \(([^)]*)\)|@(\S+)|(.*?)))? (\{.*\})\]$/
// The engine's own framing of a stored peer delivery: a lead line, maybe an envelope tag.
// UserMessage's props.text arrives without it; session.append's content arrives with it.
const LEAD = /^(?:Another Claude session|A peer session) sent a message[^\n]*:\n/
const ENVELOPE = /^<cross-session-message\b[^>]*>\n?/
const TAIL = /\n*(?:<\/cross-session-message>|\n(?:This came from another Claude session|This is from another Claude session|IMPORTANT: This is NOT from your user))[\s\S]*$/

const unframe = (text: string) => text.replace(LEAD, '').replace(ENVELOPE, '')

export function classify(text: string): Sender {
  const body = unframe(text)
  if (OWNER.test(body)) return { kind: 'owner' }
  const m = TEAMMATE.exec(body.split('\n', 1)[0] ?? '')
  if (!m) return { kind: 'peer' }
  let provenance: { actor?: { kind?: string; id?: string }; actorUserId?: string; actorDisplayName?: string } = {}
  try { provenance = JSON.parse(m[4] ?? '') } catch { /* label only */ }
  const label = m[3] === 'a workspace member' ? 'A workspace member' : m[3]
  const name = provenance.actorDisplayName || m[1] || m[2] || label || 'A teammate'
  const raw = provenance.actorUserId || provenance.actor?.id
  const id = typeof raw === 'string' && raw ? { id: raw } : {}
  if (provenance.actor?.kind === 'agent') return { kind: 'agent', name, ...id }
  if (provenance.actor?.kind === 'session') return { kind: 'session', name, ...id }
  return { kind: 'person', name, ...id }
}

/** The message as the person reads it on one row: no labels, no engine framing. */
export const rowBody = (text: string) =>
  unframe(text).replace(TAIL, '').split('\n')
    .filter(l => !l.startsWith('[Lightbulb ') && !l.startsWith('[Sent from Lightbulb')).join(' ').trim()

// Case-insensitive where the Mac's file system is: claude.md is CLAUDE.md there.
const PROTECTED = [
  /(^|\/)\.claude\/settings(\.[\w-]+)?\.json$/i,
  /(^|\/)\.claude\/hooks\//i,
  /(^|\/)CLAUDE\.md$/i,
  /(^|\/)AGENTS\.md$/i,
  /(^|\/)\.codex\//i,
]
export const protectedPath = (path: string) => PROTECTED.some(r => r.test(path))

// ponytail: a word match on the command text, not a shell parser; a teammate can still ask for
// an indirect write (a script that edits CLAUDE.md). The skill and the owner's prompts stay the fence for that.
const BASH = /(\.claude\/settings[\w.-]*\.json|\.claude\/hooks\/|CLAUDE\.md|AGENTS\.md|\.codex\/)/i
export const bashTouchesProtected = (command: string) => BASH.test(command)

export function rowLabel(s: Sender): string | null {
  switch (s.kind) {
    case 'owner': return 'You · from Lightbulb'
    case 'person': return `${s.name} · via Lightbulb`
    case 'agent': return `${s.name} · agent`
    case 'session': return `${s.name} · session`
    default: return null
  }
}

// Named ANSI colors, so the person's terminal theme picks the shades. No red: the band's errors are red.
export const PALETTE = ['cyan', 'magenta', 'green', 'yellow', 'blue', 'blueBright'] as const
export const OWNER_COLOR = 'gray'

/** One color per sender, the same every time: FNV-1a over the stable id, mod the palette. */
export function colorFor(key: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < key.length; i++) h = Math.imul(h ^ key.charCodeAt(i), 0x01000193)
  return PALETTE[(h >>> 0) % PALETTE.length]!
}

export function senderColor(s: Sender): string | undefined {
  if (s.kind === 'owner') return OWNER_COLOR
  if (s.kind === 'peer') return undefined
  return colorFor(s.id ?? s.name)
}
