export type Viewer = { userId: string; name: string; canType: boolean; typing: boolean }
/** `agent_message`: an agent asks to message this session (`userId` is the agent's id); `action` and `text` come with it. */
export type JoinRequest = { userId: string; name: string; requestId: string; kind: 'join' | 'pickup' | 'agent_message'; action?: 'send' | 'interrupt'; text?: string }
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
 *  (only its person's), or shared and waiting for a live terminal (a plain `claude`). */
export type Local = { shared: boolean; /** Stopped on this Mac; the workspace has not taken the stop yet. */ stopping?: boolean }

declare module 'claude-code' {
  interface PluginState {
    'lightbulb': { state: SessionState | null; error: string | null; continued: Continued | null; local: Local | null; shareError: string | null }
  }
}
