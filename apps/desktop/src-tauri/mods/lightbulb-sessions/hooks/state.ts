import type { SessionState } from '../types'

// The pure half of the mod. Everything that touches `$` lives in register.tsx: the engine
// follows `$` only into functions declared in the hooks module itself, never across an import.

export type Decision = { decision: 'allow' | 'decline' | 'pause' | 'unpause'; userId?: string; requestId?: string }

export function bandLine(s: SessionState): string {
  if (s.paused) return '○ Sharing paused'
  if (s.terminal === null) return '● Shared as a transcript'
  if (s.viewers.length === 0) return '● Live in Lightbulb'
  const watching = s.viewers.map(v => v.name).join(', ')
  const typing = s.viewers.filter(v => v.typing).map(v => v.name)
  return `● Live · ${watching} watching` + (typing.length ? ` · ${typing.join(', ')} typing` : '')
}
