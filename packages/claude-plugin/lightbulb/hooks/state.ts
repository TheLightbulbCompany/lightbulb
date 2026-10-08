import type { Continued, JoinRequest, NotShared, SessionState } from '../types'
import { colorFor, type Sender } from './rules'

// The pure half of the mod. Everything that touches `$` lives in register.tsx: the engine
// follows `$` only into functions declared in the hooks module itself, never across an import.

export type Decision = { decision: 'allow' | 'decline' | 'pause' | 'unpause'; userId?: string; requestId?: string }

// The band's one line in runs; a person's name carries their color (keyed on userId).
export type Run = { text: string; color?: string }

export function bandRuns(s: SessionState): Run[] {
  if (s.paused) return [{ text: '○ Sharing paused' }]
  if (s.terminal === null) return [{ text: '● Shared as a transcript' }]
  if (s.viewers.length === 0) return [{ text: '● Live in Lightbulb' }]
  const names = (vs: SessionState['viewers']) =>
    vs.flatMap((v, i): Run[] => [...(i ? [{ text: ', ' }] : []), { text: v.name, color: colorFor(v.userId) }])
  const typing = s.viewers.filter(v => v.typing)
  return [{ text: '● Live · ' }, ...names(s.viewers), { text: ' watching' },
    ...(typing.length ? [{ text: ' · ' }, ...names(typing), { text: ' typing' }] : [])]
}

export const bandLine = (s: SessionState) => bandRuns(s).map(r => r.text).join('')

export function requestLine(r: JoinRequest, count: number): string {
  const verb = r.kind === 'agent_message'
    ? (r.action === 'interrupt' ? 'wants to stop this session' : `wants to message this session: ${printable(r.text)}`)
    : r.kind === 'pickup' ? 'wants to pick this up · conversation + uncommitted changes' : 'asked to join'
  return `${r.name} ${verb}` + (count > 1 ? ` (+${count - 1} more)` : '')
}

// How the keyboard reaches the band's buttons (Claude Code 2.1.292). A Button's `hotkey` letter
// is pressed only while the band holds the keyboard: after ctrl+x tab (the engine's
// abovePrompt:focus; Esc goes back to the prompt) or a click. At the prompt the letter is typed.
// A mod cannot bind a chord of its own: a Button's `action` takes one of the engine's own
// keybinding actions and the engine refuses the whole band over any other name.
// ponytail: the default chord, written out. A person who rebound abovePrompt:focus reads the old
// one; the API hands a mod no way to ask for theirs.
export function keysHint(s: SessionState): string {
  return s.joinRequests.length ? 'ctrl+x tab, then a or d · or click' : 'ctrl+x tab, then p'
}

// The band of a session that is not shared because its folder is not approved (local only:
// nothing is registered until the person chooses to share). Its one action approves the folder,
// which shares every session in it, so the second line says so before it happens.
// (`holder/model.rs` knows these texts; keep them with it.)
export const NOT_SHARED_LINE = '○ Not shared with your workspace'
export const SHARE_LABEL = 'Share to Lightbulb'
export const SHARE_HINT = 'ctrl+x tab, then s'
export const SHARING_LINE = '○ Sharing with your workspace…'
export const shareNote = (n: NotShared) => `Shares every session in ${n.folder}`

// The app's answer for such a session (403): its folder's name came from this Mac's own disk.
export function parseNotShared(text: string): NotShared | null {
  try {
    const v = JSON.parse(text) as { error?: unknown; folder?: unknown }
    const folder = printable(v.folder)
    return v.error === 'folder_not_approved' && folder ? { folder } : null
  } catch {
    return null
  }
}

const printable = (s: unknown) => (typeof s === 'string' ? s.replace(/[\u0000-\u001f\u007f-\u009f]/g, '').trim().slice(0, 80) : '')

/** Who typed the live terminal's last input, when a teammate did. Their name came from a server. */
export function typist(s: SessionState | null | undefined): Sender | null {
  const [id, name] = [printable(s?.typedBy?.userId), printable(s?.typedBy?.name)]
  return s && !s.paused && s.terminal !== null && id && name ? { kind: 'typed', name, id } : null
}

// Prompts a teammate typed and submitted in the live terminal, by their text: a UserMessage row
// carries no id, and a hook may not set a prompt's origin.
// ponytail: in memory and keyed on the text. The owner submitting the same text later drops the
// label from both rows; a reload or a resumed session forgets all of them (the rows draw as the
// owner's own). A header in the stored text would survive those, but the model would read it.
const TYPED_CAP = 200
export function rememberTyped(rows: Map<string, Sender>, text: string, who: Sender | null): void {
  const key = text.trim()
  rows.delete(key)
  if (!who || !key) return
  rows.set(key, who)
  if (rows.size > TYPED_CAP) rows.delete(rows.keys().next().value!)
}

// The marker `lightbulb continue` writes before it starts Claude. Its text came from a server.
export function parseContinued(text: string): Continued | null {
  try {
    const v = JSON.parse(text) as { from?: unknown; title?: unknown }
    const from = printable(v.from)
    return from ? { from, title: printable(v.title) || null } : null
  } catch {
    return null
  }
}

export const continuedLine = (c: Continued) => `Continued from ${c.from}'s session · Share yours (thelightbulb.company)`
