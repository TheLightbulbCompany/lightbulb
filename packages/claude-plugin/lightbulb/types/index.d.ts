export type Viewer = { userId: string; name: string; canType: boolean; typing: boolean }
export type JoinRequest = { userId: string; name: string; requestId: string; kind: 'join' | 'pickup' }
export type Continued = { from: string; title: string | null }
export type TypedBy = { userId: string; name: string }
export type SessionState = {
  sessionId: string
  paused: boolean
  terminal: 'held' | 'attach' | null
  viewers: Viewer[]
  /** People on the session's "Anyone with the link" page: a count, never who. Absent from an older app. */
  linkViewers?: number
  joinRequests: JoinRequest[]
  approvalWaiting: boolean
  /** The teammate whose keys were the live terminal's last input; absent when the owner's were. */
  typedBy?: TypedBy
}

/** A session on a Mac that shares sessions, with nothing in the workspace to show: not shared
 *  (only its person's), or stopping. A shared session with no live terminal (a plain `claude`) is
 *  shared as its conversation; only an app from before 2026-10-11 still says `shared` here. */
export type Local = { shared: boolean; /** Stopped on this Mac; the workspace has not taken the stop yet. */ stopping?: boolean }

declare module 'claude-code' {
  interface PluginState {
    'lightbulb': { state: SessionState | null; error: string | null; continued: Continued | null; local: Local | null; shareError: string | null }
  }
}
