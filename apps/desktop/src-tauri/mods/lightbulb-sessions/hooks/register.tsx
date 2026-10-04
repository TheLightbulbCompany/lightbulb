import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { SessionState } from '../types'
import { bandLine, type Decision } from './state'

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

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const refresh = async () => {
      const s = await fetchState($)
      await update($, sharing, () => s)
    }
    await refresh()
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
