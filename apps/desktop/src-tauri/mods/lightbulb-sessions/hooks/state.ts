import type { JoinRequest, SessionState } from '../types'
import { colorFor } from './rules'

// The pure half of the mod. Everything that touches `$` lives in register.tsx: the engine
// follows `$` only into functions declared in the hooks module itself, never across an import.

export type Decision = { decision: 'allow' | 'decline' | 'pause' | 'unpause'; userId?: string; requestId?: string }

// The band's one line in runs; a person's name carries their color (keyed on userId).
export type Run = { text: string; color?: string }

export function bandRuns(s: SessionState): Run[] {
  if (s.paused) return [{ text: s.writerConnected ? '○ Sharing paused · resume in the Lightbulb session view' : '○ Sharing paused' }]
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
  const verb = r.kind === 'pickup' ? 'wants to pick this up · shares the whole conversation and uncommitted changes with the workspace' : 'asked to join'
  return `${r.name} ${verb}` + (count > 1 ? ` (+${count - 1} more)` : '')
}
