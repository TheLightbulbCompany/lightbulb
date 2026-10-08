import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { Continued, Local, SessionState } from '../types'
import { bandRuns, continuedLine, GO_LIVE_LINE, keysHint, ONLY_YOU_LINE, parseContinued, rememberTyped, requestLine, SHARE_HINT, SHARE_LABEL, STOPPING_LINE, typist, type Decision } from './state'
import { bashTouchesProtected, classify, colorFor, protectedPath, rowBody, rowLabel, senderColor, type Sender } from './rules'

export const sharing = atom({ plugin: 'lightbulb', key: 'state' } as const, null as SessionState | null)
// A refused decision's message, and the decision it was about: it stays only while that decision
// still applies (the request is still first, or sharing is still in the state it would change).
type Problem = { text: string; about: Decision }
const problem = atom({ plugin: 'lightbulb', key: 'error' } as const, null as Problem | null)
// Where this session was continued from, read once from the marker `lightbulb continue` wrote.
const continued = atom({ plugin: 'lightbulb', key: 'continued' } as const, null as Continued | null)
// Not shared, or shared and waiting for a live terminal; and what the app said to a refused Share.
const local = atom({ plugin: 'lightbulb', key: 'local' } as const, null as Local | null)
const shareError = atom({ plugin: 'lightbulb', key: 'shareError' } as const, null as string | null)
const NO_APP = 'Lightbulb is not sharing on this Mac. Open the Lightbulb app and turn on session sharing.'
const NO_ANSWER = 'Lightbulb did not answer. Open the Lightbulb app, then try again.'
// One decision at a time: a second press while one is out would send the same answer twice.
// A module variable on purpose: it is set and tested in one synchronous step.
let deciding = false
const REFUSED = 'Lightbulb did not take that. Try again in the app.'

// Prompts a teammate typed in the live terminal (state.ts rememberTyped). A module variable on
// purpose: prompt.submit writes it before the row draws.
const typedRows = new Map<string, Sender>()
const TYPED_WAIT_MS = 1500 // a prompt never waits longer than this on the app

const POLL_MS = 2000
const GRACE_MS = 60_000 // a missed poll keeps the last good band this long: the app may just be busy
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

async function continuedMarker($: EngineInterface): Promise<Continued | null> {
  try {
    const home = await $.env.get('HOME')
    if (!home) return null
    const text = await $.fs.read(`${home}/.lightbulb/continued/${await $.session.id()}.json`)
    return typeof text === 'string' ? parseContinued(text) : null
  } catch {
    return null // not a continued session
  }
}

// state null: nothing to show (no socket yet, or the app says this session is not shared).
// state undefined: no answer (the app is not running, busy, or refused the call); `message` is
// what the app said when it refused, if it said anything (e.g. "That request is no longer waiting.").
// local: the app knows this session and has nothing of it in the workspace (see `Local`).
type Reply = { state: SessionState | null | undefined; message?: string; local?: Local }

// No timeout to set: HttpInit has none, and a hook's budget stops while a `$` call is in flight,
// so a slow decide stays open.
async function call($: EngineInterface, path: string, body?: Decision): Promise<Reply> {
  const socket = await socketPath($)
  // The app removes the socket when sharing stops or it quits: hide at once, not after the grace.
  if (!socket || !(await $.fs.exists(socket))) return { state: null }
  try {
    const res = await $.http.fetch(`${APP}${path}`, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      socketPath: socket,
    })
    if (res.status === 404) return { state: null }
    if (res.status !== 200) return { state: undefined, message: said(res.text) }
    // A shared session's state names it; any other answer is the local one (see `Local`).
    const answer = JSON.parse(res.text) as Partial<SessionState & Local>
    return typeof answer.sessionId === 'string' ? { state: answer as SessionState } : { state: null, local: { shared: answer.shared === true, stopping: answer.stopping === true } }
  } catch {
    return { state: undefined }
  }
}

function said(text: string): string | undefined {
  try {
    const m = (JSON.parse(text) as { message?: unknown }).message
    return typeof m === 'string' && m.trim() ? m.slice(0, 200) : undefined
  } catch {
    return undefined
  }
}

export async function fetchState($: EngineInterface): Promise<SessionState | null | undefined> {
  return (await call($, '/v1/state')).state
}

export async function decide($: EngineInterface, body: Decision): Promise<Reply> {
  return call($, '/v1/decide', body)
}

// Shares this session with the workspace, whole. Null when the app took it; else the one line to show.
async function share($: EngineInterface): Promise<string | null> {
  const socket = await socketPath($)
  if (!socket || !(await $.fs.exists(socket))) return NO_APP
  try {
    // (The app also knows the session by this process, so a missing id still shares it.)
    const sessionId = await $.session.id().catch(() => undefined)
    const res = await $.http.fetch(`${APP}/v1/share`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sessionId }),
      socketPath: socket,
    })
    return res.status === 200 ? null : said(res.text) ?? REFUSED
  } catch {
    return NO_ANSWER
  }
}

function applies(about: Decision, s: SessionState | null): boolean {
  if (!s) return false
  if (about.decision === 'pause') return !s.paused
  if (about.decision === 'unpause') return s.paused
  return s.joinRequests[0]?.requestId === about.requestId
}

// A turn a teammate's message started (or joined) may not change config; cleared when the main
// turn ends. A module variable on purpose: a reload mid-turn clears it (fail-open for one turn,
// accepted; the skill still applies). Only a Lightbulb teammate contribution (a person, an agent or a
// shared session) sets it: a plain peer (another Claude session, a subagent's hand-back) is the owner's own.
let teammateTurn = false
const TEAMMATE_KINDS = new Set(['person', 'agent', 'session'])
const refuse = (what: string) => ({ deny: `Lightbulb: a teammate's request cannot change ${what}. Ask your user to make this change.` })

export const register: Register = on => {
  // prompt.submit sees a peer's message both as a turn of its own and folded into a running turn
  // (PromptOrigin 'peer'; PromptSubmitInput.turnId). session.append does not: a folded delivery is
  // appended as an engine attachment row.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'peer' && TEAMMATE_KINDS.has(classify(e.text).kind)) teammateTurn = true
    // A prompt typed at the terminal: the owner's own keys, or a teammate's through Lightbulb.
    // Asked once, at the Enter, and only where the last poll showed a live shared terminal, so an
    // unshared session never waits on the app.
    if (e.origin.kind === 'composer') {
      const s = await read($, sharing)
      const live = s && !s.paused && s.terminal !== null
      const now = live ? await Promise.race([fetchState($), $.clock.sleep(TYPED_WAIT_MS, { signal: next.signal }).then(() => undefined, () => undefined)]) : null
      rememberTyped(typedRows, e.text, typist(now))
    }
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
    const sender = classify(e.props.text)
    const label = rowLabel(sender)
    if (!label) return next(e)
    const body = rowBody(e.props.text)
    const color = senderColor(sender)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box borderStyle="round" borderColor={color}>
        <Text bold color={color}>{label}</Text>
        <Text>  {body.length > 200 ? body.slice(0, 199) + '…' : body}</Text>
      </Box>
    )
  })

  // A prompt a teammate typed and submitted in the live terminal: the engine's row would read as
  // the owner's own. Drawn expanded too (the whole prompt is shown, so ctrl+o loses nothing).
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'composer' } } }, async ($, e, next) => {
    const who = typedRows.get(e.props.text.trim())
    if (!who) return next(e)
    const color = senderColor(who)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box borderStyle="round" borderColor={color} flexDirection="column">
        <Text bold color={color}>{rowLabel(who)}</Text>
        <Text>{e.props.text}</Text>
      </Box>
    )
  })

  on('session.start', async ($, e, next) => {
    let goodAt = -Infinity
    const refresh = async () => {
      const answer = await call($, '/v1/state')
      const s = answer.state
      const now = await $.clock.now()
      if (s) goodAt = now
      else if (s === undefined && now - goodAt < GRACE_MS) return
      await update($, sharing, () => s ?? null)
      await update($, local, () => answer.local ?? null)
      if (!answer.local || answer.local.shared) await update($, shareError, () => null)
      // A poll that resolves what a refusal was about clears its line.
      await update($, problem, p => (p && applies(p.about, s ?? null) ? p : null))
    }
    // Never wait on the app: this runs in every Claude session on the Mac, and a stalled socket
    // must not hold session start. The band appears when the first answer lands.
    $.clock.after(0, async () => {
      const c = await continuedMarker($)
      if (c) await update($, continued, () => c)
    })
    $.clock.after(0, refresh)
    $.clock.every(POLL_MS, refresh)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, sharing)
    const l = s ? null : await read($, local)
    if (l?.shared || l?.stopping) {
      const { Text } = $.ui.resolve(e)
      return <Text dimColor>{l.shared ? GO_LIVE_LINE : STOPPING_LINE}</Text>
    }
    if (l) {
      // Only its person's: nothing of it leaves this Mac, and this line is local.
      const { Box, Button, Text } = $.ui.resolve(e)
      const press = async () => {
        if (deciding) return
        deciding = true
        try {
          const refused = await share($)
          await update($, shareError, () => refused)
          if (!refused) {
            const after = await call($, '/v1/state')
            if (after.state !== undefined) {
              await update($, sharing, () => after.state ?? null)
              await update($, local, () => after.local ?? null)
            }
          }
        } finally {
          deciding = false
        }
      }
      const refused = await read($, shareError)
      return (
        <Box flexDirection="column">
          <Box>
            <Text dimColor>{ONLY_YOU_LINE} </Text>
            <Button key="share" hotkey="s" label={SHARE_LABEL} onPress={press} />
            {e.surface === 'terminal' ? <Text dimColor> {SHARE_HINT}</Text> : null}
          </Box>
          {refused && <Text color="red">{refused}</Text>}
        </Box>
      )
    }
    if (!s) {
      // Not shared itself: a continued session says where it came from (spec §3.6).
      const c = await read($, continued)
      if (!c) return next(e)
      const { Text } = $.ui.resolve(e)
      return <Text dimColor>{continuedLine(c)}</Text>
    }
    const { Box, Button, Text } = $.ui.resolve(e)
    const act = (body: Decision) => async () => {
      if (deciding) return
      deciding = true
      try {
        const { state: after, message, local: stopped } = await decide($, body)
        if (after) await update($, sharing, () => after) // never write back the draw-time `s`: the poll may be newer
        // Stop sharing stops it on this Mac: the app answers that the session is only its person's.
        if (stopped) {
          await update($, sharing, () => null)
          await update($, local, () => stopped)
        }
        await update($, problem, () => (after || stopped ? null : { text: message ?? REFUSED, about: body }))
      } finally {
        deciding = false
      }
    }
    const ask = s.joinRequests[0]
    const p = await read($, problem)
    const err = p && applies(p.about, s) ? p.text : null
    // The terminal only: a desktop draws native buttons and has no chord. One hint, on the row
    // whose answer is waited for.
    const hint = e.surface === 'terminal' ? <Text dimColor> {keysHint(s)}</Text> : null
    return (
      <Box flexDirection="column">
        <Box>
          <Text dimColor>{bandRuns(s).map(r => r.color ? <Text color={r.color}>{r.text}</Text> : r.text)} </Text>
          {s.paused
 ? <Button key="resume" hotkey="s" label={SHARE_LABEL} onPress={act({ decision: 'unpause' })} />
            : <Button key="pause" hotkey="p" label="Stop sharing" onPress={act({ decision: 'pause' })} />}
          {!ask && hint}
        </Box>
        {ask && (
          <Box>
            <Text><Text color={colorFor(ask.userId)}>{ask.name}</Text>{requestLine(ask, s.joinRequests.length).slice(ask.name.length)} </Text>
            <Button key="allow" hotkey="a" variant="primary" label="Allow" onPress={act({ decision: 'allow', userId: ask.userId, requestId: ask.requestId })} />
            <Button key="decline" hotkey="d" label="Decline" onPress={act({ decision: 'decline', userId: ask.userId, requestId: ask.requestId })} />
            {hint}
          </Box>
        )}
        {s.approvalWaiting && <Text dimColor>Waiting on you here or in Lightbulb</Text>}
        {err && <Text color="red">{err}</Text>}
      </Box>
    )
  })
}
