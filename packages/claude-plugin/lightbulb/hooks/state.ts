import type { Continued, JoinRequest, SessionState } from '../types'
import { colorFor, type Sender } from './rules'

// The pure half of the mod. Everything that touches `$` lives in register.tsx: the engine
// follows `$` only into functions declared in the hooks module itself, never across an import.

export type Decision = { decision: 'allow' | 'decline' | 'pause' | 'unpause'; userId?: string; requestId?: string }

// The band's one line in runs; a person's name carries their color (keyed on userId).
export type Run = { text: string; color?: string }

export function bandRuns(s: SessionState): Run[] {
  // Its person chose Only me in the workspace: the same line as a session that was never shared.
  if (s.paused) return [{ text: ONLY_YOU_LINE }]
  // No terminal: it runs inside an app, and members read its conversation.
  if (s.terminal === null) return [{ text: '● Shared in Lightbulb' }]
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
  return s.joinRequests.length ? 'ctrl+x tab, then a or d · or click' : s.paused ? SHARE_HINT : 'ctrl+x tab, then p'
}

// The band of a session with nothing in the workspace (local only: nothing is registered).
// Not shared: its one action shares the whole session, from its start. Shared, in a plain
// terminal: nothing is uploaded until ← moves it to a live one, and the app cannot press it.
export const ONLY_YOU_LINE = '○ Only you'
export const SHARE_LABEL = 'Share to Lightbulb'
export const SHARE_HINT = 'ctrl+x tab, then s'
export const GO_LIVE_LINE = '○ Press ← once to go live'
// Stopped here (nothing more is sent), and the app has not reached the workspace with it yet.
export const STOPPING_LINE = '○ Stopping… teammates may still see earlier output'

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
