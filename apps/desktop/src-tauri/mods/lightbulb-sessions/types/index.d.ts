export type Viewer = { userId: string; name: string; canType: boolean; typing: boolean }
export type JoinRequest = { userId: string; name: string; requestId: string; kind: 'join' | 'pickup' }
export type SessionState = {
  sessionId: string
  paused: boolean
  terminal: 'held' | 'attach' | null
  viewers: Viewer[]
  joinRequests: JoinRequest[]
  approvalWaiting: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'lightbulb-sessions': { state: SessionState | null; error: string | null; confirming: boolean }
  }
}
