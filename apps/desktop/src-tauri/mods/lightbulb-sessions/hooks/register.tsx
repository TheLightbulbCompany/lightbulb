import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { SessionState } from '../types'
import { bandLine, type Decision } from './state'
import { bashTouchesProtected, classify, protectedPath, rowBody, rowLabel } from './rules'

export const sharing = atom({ plugin: 'lightbulb-sessions', key: 'state' } as const, null as SessionState | null)
const problem = atom({ plugin: 'lightbulb-sessions', key: 'error' } as const, null as string | null)

const POLL_MS = 2000
const APP = 'http://lightbulb'

// These take `$`, so they live here: the engine follows `$` only into functions this module declares.
async function socketPath($: EngineInterface): Promise<string | null> {
  try {
    const text = await $.fs.read(`${$.plugin.root}/socket`)
    return typeof text === 'string' ? text.trim() || null : null
  } catch {
    return null // the socket path is not written yet: show nothing
  }
}

async function call($: EngineInterface, path: string, body?: Decision): Promise<SessionState | null> {
  const socket = await socketPath($)
  if (!socket) return null
  try {
    const res = await $.http.fetch(`${APP}${path}`, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      socketPath: socket,
    })
    if (res.status !== 200) return null
    return JSON.parse(res.text) as SessionState
  } catch {
    return null // the app is not running: show nothing
  }
}

export async function fetchState($: EngineInterface): Promise<SessionState | null> {
  return call($, '/v1/state')
}

export async function decide($: EngineInterface, body: Decision): Promise<SessionState | null> {
  return call($, '/v1/decide', body)
}

// A turn a teammate's message started (or joined) may not change config; cleared when the main
// turn ends. A module variable on purpose: a reload mid-turn clears it (fail-open for one turn,
// accepted; the skill still applies). Any peer but the owner's own Lightbulb message sets it.
let teammateTurn = false
const refuse = (what: string) => ({ deny: `Lightbulb: a teammate's request cannot change ${what}. Ask your user to make this change.` })

export const register: Register = on => {
  // prompt.submit sees a peer's message both as a turn of its own and folded into a running turn
  // (PromptOrigin 'peer'; PromptSubmitInput.turnId). session.append does not: a folded delivery is
  // appended as an engine attachment row.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'peer' && classify(e.text).kind !== 'owner') teammateTurn = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) teammateTurn = false // a subagent's run ending is not the teammate's turn ending
    return next(e)
  })

  on('tool.call', { tool: 'Edit' }, ($, e, next) =>
    teammateTurn && protectedPath(e.file_path) ? refuse(e.file_path) : next(e))
  on('tool.call', { tool: 'Write' }, ($, e, next) =>
    teammateTurn && protectedPath(e.file_path) ? refuse(e.file_path) : next(e))
  on('tool.call', { tool: 'NotebookEdit' }, ($, e, next) =>
    teammateTurn && protectedPath(e.notebook_path) ? refuse(e.notebook_path) : next(e))
  on('tool.call', { tool: 'Bash' }, ($, e, next) =>
    teammateTurn && bashTouchesProtected(e.command) ? refuse('that file') : next(e))

  // Redrawn for the person only: the model still reads the labelled text.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'peer' } } }, async ($, e, next) => {
    if (e.props.isExpanded) return next(e)
    const label = rowLabel(classify(e.props.text))
    if (!label) return next(e)
    const body = rowBody(e.props.text)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text bold>{label}</Text>
        <Text>  {body.length > 200 ? body.slice(0, 199) + '…' : body}</Text>
      </Box>
    )
  })

  on('session.start', async ($, e, next) => {
    const refresh = async () => {
      const s = await fetchState($)
      await update($, sharing, () => s)
    }
    // Never wait on the app: this runs in every Claude session on the Mac, and a stalled socket
    // must not hold session start. The band appears when the first answer lands.
    $.clock.after(0, refresh)
    $.clock.every(POLL_MS, refresh)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, sharing)
    if (!s) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const act = (body: Decision) => async () => {
      const after = await decide($, body)
      if (after) await update($, sharing, () => after) // never write back the draw-time `s`: the poll may be newer
      await update($, problem, () => (after ? null : 'Lightbulb did not take that. Try again in the app.'))
    }
    const ask = s.joinRequests[0]
    const err = await read($, problem)
    return (
      <Box flexDirection="column">
        <Box>
          <Text dimColor>{bandLine(s)} </Text>
          {s.paused
            ? <Button key="resume" label="Resume sharing" onPress={act({ decision: 'unpause' })} />
            : <Button key="pause" label="Pause" onPress={act({ decision: 'pause' })} />}
        </Box>
        {ask && (
          <Box>
            <Text>{ask.name} asked to join{s.joinRequests.length > 1 ? ` (+${s.joinRequests.length - 1} more)` : ''} </Text>
            <Button key="allow" variant="primary" label="Allow" onPress={act({ decision: 'allow', userId: ask.userId, requestId: ask.requestId })} />
            <Button key="decline" label="Decline" onPress={act({ decision: 'decline', userId: ask.userId, requestId: ask.requestId })} />
          </Box>
        )}
        {s.approvalWaiting && <Text dimColor>Waiting on you here or in Lightbulb</Text>}
        {err && <Text color="red">{err}</Text>}
      </Box>
    )
  })
}
