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
