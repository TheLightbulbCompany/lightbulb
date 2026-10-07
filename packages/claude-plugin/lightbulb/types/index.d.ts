export type Viewer = { userId: string; name: string; canType: boolean; typing: boolean }
export type JoinRequest = { userId: string; name: string; requestId: string; kind: 'join' | 'pickup' }
export type Continued = { from: string; title: string | null }
export type TypedBy = { userId: string; name: string }
export type SessionState = {
  sessionId: string
  paused: boolean
  terminal: 'held' | 'attach' | null
  viewers: Viewer[]
  joinRequests: JoinRequest[]
  approvalWaiting: boolean
  /** The teammate whose keys were the live terminal's last input; absent when the owner's were. */
  typedBy?: TypedBy
}

declare module 'claude-code' {
  interface PluginState {
    'lightbulb': { state: SessionState | null; error: string | null; continued: Continued | null }
  }
}
