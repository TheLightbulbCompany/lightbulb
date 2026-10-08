import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'
import { bandLine, bandRuns, continuedLine, GO_LIVE_LINE, keysHint, ONLY_YOU_LINE, parseContinued, rememberTyped, STOPPING_LINE, typist } from './state'
import { colorFor, type Sender } from './rules'
import type { SessionState } from '../types'

const P = 'lightbulb'
const SOCKET = '/tmp/lightbulb-test/mod.sock'
const ENGINE = '(the engine draws its own band)'
const SURFACES = ['terminal', 'desktop'] as const
const BAND = {
  plugin: P,
  component: 'AbovePrompt' as const,
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 9 }, view: {} },
}

const base: SessionState = { sessionId: 's', paused: false, terminal: 'held', viewers: [], joinRequests: [], approvalWaiting: false }

type Call = { url: string; method: string; body: unknown; socketPath: string | undefined }

// The world beneath the mod: the socket file, the app over the socket, the session, and the engine's own band.
// A `$` call's bottom answers `{ value }` or `{ deny }`; a deny is what the mod's call rejects with.
type Reply = { status: number; body: unknown }
type Answer = (path: string, body: unknown) => Reply | 'refuse' | 'hang' | 'held'
// `held`: the reply the test settles itself (an Allow the owner is still answering on the Mac).
function world(on: On, answer: Answer, socketFile = true, held?: Promise<Reply>) {
  const calls: Call[] = []
  const socket = { live: true } // whether the app's socket file is there
  const clock = mock.clock(on)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('fs.exists', ($, e) => ({ value: e.path === SOCKET && socket.live }))
  on('fs.read', ($, e) => {
    if (socketFile && e.path.endsWith('/lightbulb/socket')) return { value: `${SOCKET}\n` }
    return { deny: `no file ${e.path}` }
  })
  on('http.fetch', ($, e) => {
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    calls.push({ url: e.url, method: e.init?.method ?? 'GET', body, socketPath: e.init?.socketPath })
    const a = answer(new URL(e.url).pathname, body)
    if (a === 'refuse') return { deny: 'connect ECONNREFUSED' }
    if (a === 'hang') return new Promise<never>(() => {}) // accepts the connection, never answers
    const reply = (r: Reply) => ({ value: { status: r.status, ok: r.status >= 200 && r.status < 300, headers: {}, text: JSON.stringify(r.body) } })
    if (a === 'held') return held!.then(reply)
    return reply(a)
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return Text({ children: ENGINE })
  })
  return { calls, clock, socket }
}

test('band says live and nobody watching', () => {
  expect(bandLine(base)).toBe('● Live in Lightbulb')
})

test('band names watchers and typists', () => {
  const s = { ...base, viewers: [
    { userId: 'a', name: 'Aden Kim', canType: true, typing: true },
    { userId: 'm', name: 'Mira Shah', canType: false, typing: false },
  ] }
  expect(bandLine(s)).toBe('● Live · Aden Kim, Mira Shah watching · Aden Kim typing')
  // each name in its person's color, keyed on userId; the rest plain
  expect(bandRuns(s).filter(r => r.color)).toEqual([
    { text: 'Aden Kim', color: colorFor('a') }, { text: 'Mira Shah', color: colorFor('m') }, { text: 'Aden Kim', color: colorFor('a') },
  ])
})

test('the band draws watcher names and the asker in their colors', async ($, on) => {
  const s: SessionState = { ...asking, viewers: [{ userId: 'm', name: 'Mira Shah', canType: true, typing: true }] }
  const { clock } = world(on, () => ({ status: 200, body: s }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    const colored = (await ui.findAll({ type: 'Text' })).filter(t => t.props.color).map(t => [t.text, t.props.color])
    expect(colored).toEqual([['Mira Shah', colorFor('m')], ['Mira Shah', colorFor('m')], ['Aden Kim', colorFor('u1')]])
    expect(await ui.find({ type: 'Text', text: /Mira Shah watching/ })).toBeDefined()
    await ui.unmount()
  }
})

test('band says shared for a session with no terminal, and Only you wins', () => {
  expect(bandLine({ ...base, terminal: null })).toBe('● Shared in Lightbulb')
  expect(bandLine({ ...base, paused: true })).toBe(ONLY_YOU_LINE)
})

test('band shows nothing when the app does not answer', async ($, on) => {
  let answer: Answer = () => 'refuse'
  const { calls, clock } = world(on, (path, body) => answer(path, body))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(calls.length).toBeGreaterThan(0)
  expect(calls[0]).toMatchObject({ url: 'http://lightbulb/v1/state', method: 'GET', socketPath: SOCKET })
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE] })
    expect(await ui.findAll({ type: 'Button' })).toHaveLength(0)
    expect(await ui.find({ text: /Live|Shared|paused|Lightbulb/ })).toBeUndefined()
    await ui.unmount()
  }
  // Nothing threw: the session.start hook got as far as arming the poll, so the band comes back with the app.
  answer = () => ({ status: 200, body: base })
  await clock.advance(2000)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: '● Live in Lightbulb' })).toBeDefined()
    await ui.unmount()
  }
})

test('session start does not wait for an app that never answers', async ($, on) => {
  const { calls, clock } = world(on, () => 'hang')
  const started = await Promise.race([
    $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true }).then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 1000)),
  ])
  expect(started).toBe(true)
  await clock.settle()
  expect(calls).toHaveLength(1) // the first fetch did go out, after start
})

test('band shows nothing and calls nothing before the socket path is written', async ($, on) => {
  const { calls, clock } = world(on, () => ({ status: 200, body: base }), false)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(calls).toHaveLength(0)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE] })
    await ui.unmount()
  }
})

test('band shows nothing when the session is not shared', async ($, on) => {
  const { clock } = world(on, () => ({ status: 404, body: { error: 'not_shared' } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE] })
    expect(await ui.findAll({ type: 'Button' })).toHaveLength(0)
    await ui.unmount()
  }
})

const asking: SessionState = { ...base, joinRequests: [{ userId: 'u1', name: 'Aden Kim', requestId: 'r1', kind: 'join' }] }

test('Allow posts the decision and the request leaves the band', async ($, on) => {
  const { calls, clock } = world(on, path => ({ status: 200, body: path === '/v1/decide' ? base : asking }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: 'Aden Kim asked to join' }))).toBeDefined()
    expect((await ui.find({ text: '● Live in Lightbulb' }))).toBeDefined()
    await ui.press({ key: 'allow' })
    const post = calls.filter(c => c.method === 'POST')
    expect(post[post.length - 1]).toEqual({
      url: 'http://lightbulb/v1/decide', method: 'POST', socketPath: SOCKET,
      body: { decision: 'allow', userId: 'u1', requestId: 'r1' },
    })
    expect(await ui.find({ key: 'allow' })).toBeUndefined()
    await ui.unmount()
    // the next surface starts from the request again
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.settle()
  }
})

test("a refused decision shows the app's message and keeps the request", async ($, on) => {
  const { clock } = world(on, path => path === '/v1/decide'
    ? { status: 409, body: { error: 'stale', message: 'That request is no longer waiting.' } }
    : { status: 200, body: asking })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    await ui.press({ key: 'allow' })
    expect(await ui.find({ type: 'Text', text: 'That request is no longer waiting.' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Lightbulb did not take that. Try again in the app.' })).toBeUndefined()
    expect(await ui.find({ key: 'allow' })).toBeDefined()
    await ui.unmount()
  }
})

test('a refusal with no message shows the generic error line', async ($, on) => {
  const { clock } = world(on, path => path === '/v1/decide' ? { status: 500, body: 'oops' } : { status: 200, body: asking })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'allow' })
  expect(await ui.find({ type: 'Text', text: 'Lightbulb did not take that. Try again in the app.' })).toBeDefined()
})

test('an Allow out blocks a second press', async ($, on) => {
  let answer: (v: { status: number; body: unknown }) => void = () => {}
  const held = new Promise<{ status: number; body: unknown }>(r => { answer = r })
  const { calls, clock } = world(on, path => path === '/v1/decide' ? 'held' : { status: 200, body: asking }, true, held)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const first = ui.press({ key: 'allow' })
  await clock.settle()
  const sent = calls.filter(c => c.url.endsWith('/v1/decide')).length
  await ui.press({ key: 'allow' })
  expect(calls.filter(c => c.url.endsWith('/v1/decide')).length).toBe(sent)
  answer({ status: 200, body: base })
  await first
  expect(sent).toBe(1)
})

test('a poll that resolves a refused decision clears its error line', async ($, on) => {
  let state: SessionState = asking
  const { clock } = world(on, path => path === '/v1/decide'
    ? { status: 409, body: { error: 'unavailable', message: 'Lightbulb is unreachable.' } }
    : { status: 200, body: state })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'allow' })
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeDefined()
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeDefined() // still about the request shown
  state = { ...asking, joinRequests: [{ userId: 'u2', name: 'Mira Shah', requestId: 'r2', kind: 'join' }] }
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeUndefined() // that request was answered elsewhere
  await ui.press({ key: 'pause' })
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeDefined()
  state = { ...base, paused: true }
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeUndefined() // paused after all
  state = base
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: 'Lightbulb is unreachable.' })).toBeUndefined() // cleared, not hidden
  await ui.unmount()
})

test('Stop sharing posts pause, and an Only you band offers Share to Lightbulb', async ($, on) => {
  const { calls, clock } = world(on, path => ({ status: 200, body: path === '/v1/decide' ? { ...base, paused: true } : base }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'pause' })
  expect(calls[calls.length - 1]?.body).toEqual({ decision: 'pause' })
  expect(await ui.find({ text: new RegExp(`^${ONLY_YOU_LINE}`) })).toBeDefined()
  expect(await ui.find({ key: 'resume' })).toMatchObject({ props: { label: 'Share to Lightbulb' } })
})

test('a missed poll keeps the last band for a minute, then hides it', async ($, on) => {
  let answer: Answer = () => ({ status: 200, body: base })
  const { clock } = world(on, (path, body) => answer(path, body))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const shown = async () => {
    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    const found = await ui.find({ type: 'Text', text: '● Live in Lightbulb' })
    await ui.unmount()
    return found !== undefined
  }
  expect(await shown()).toBe(true)
  answer = () => 'hang' // the app accepts and never answers, as a starved socket did
  await clock.advance(2000)
  answer = () => 'refuse'
  await clock.advance(54000)
  expect(await shown()).toBe(true)
  answer = () => ({ status: 500, body: {} })
  await clock.advance(6000)
  expect(await shown()).toBe(false)
})

test('a not-shared answer hides the band at once', async ($, on) => {
  let answer: Answer = () => ({ status: 200, body: base })
  const { clock } = world(on, (path, body) => answer(path, body))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  answer = () => ({ status: 404, body: { error: 'not_shared' } })
  await clock.advance(2000)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE] })
})

const ONLY_YOU: Reply = { status: 200, body: { shared: false } }

test('a session that is not shared says Only you and offers Share to Lightbulb', async ($, on) => {
  const { calls, clock } = world(on, () => ONLY_YOU)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: new RegExp(`^${ONLY_YOU_LINE}`) })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: / ctrl\+x tab, then s$/ })).toBeDefined()
  const buttons = await ui.findAll({ type: 'Button' })
  expect(buttons.map(b => [b.key, b.props.hotkey, b.props.label])).toEqual([['share', 's', 'Share to Lightbulb']])
  expect(calls.every(c => c.method === 'GET')).toBe(true) // nothing is asked of the app until the person chooses
  await ui.unmount()
  const desktop = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await desktop.find({ text: /ctrl\+x tab/ })).toBeUndefined() // a desktop draws a native button
  await desktop.unmount()
})

test('Share to Lightbulb names this session to the app and shows the shared band at once', async ($, on) => {
  let state: Reply = ONLY_YOU
  const { calls, clock } = world(on, path => (path === '/v1/share' ? ((state = { status: 200, body: base }), { status: 200, body: { shared: true, waiting: false } }) : state))
  on('session.id', () => ({ value: 'conversation-1' }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'share' })
  const asked = calls.find(c => c.url === 'http://lightbulb/v1/share')!
  expect(asked).toMatchObject({ method: 'POST', socketPath: SOCKET })
  expect(asked.body).toEqual({ sessionId: 'conversation-1' })
  expect(await ui.find({ type: 'Text', text: '● Live in Lightbulb' })).toBeDefined() // no wait for the next poll
  expect(await ui.find({ key: 'pause' })).toBeDefined()
  await ui.unmount()
})

test('a shared session in a plain terminal says how to go live and offers nothing', async ($, on) => {
  const { clock } = world(on, () => ({ status: 200, body: { shared: true } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(GO_LIVE_LINE).toBe('○ Press ← once to go live')
  expect(await ui.find({ type: 'Text', text: GO_LIVE_LINE })).toBeDefined()
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(0)
  await ui.unmount()
})

test('Stop sharing: the band says Only you and offers Share again', async ($, on) => {
  let state: Reply = { status: 200, body: base }
  const { clock } = world(on, path => (path === '/v1/decide' ? (state = ONLY_YOU) : state))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'pause' })
  expect(await ui.find({ type: 'Text', text: new RegExp(`^${ONLY_YOU_LINE}`) })).toBeDefined()
  expect(await ui.find({ key: 'share' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /did not take that/ })).toBeUndefined()
  await ui.unmount()
})

test('a stop the workspace has not taken yet says Stopping, not Only you', async ($, on) => {
  let state: Reply = { status: 200, body: base }
  const { clock } = world(on, path => (path === '/v1/decide' ? (state = { status: 200, body: { shared: false, stopping: true } }) : state))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'pause' })
  expect(await ui.find({ type: 'Text', text: STOPPING_LINE })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: new RegExp(`^${ONLY_YOU_LINE}`) })).toBeUndefined()
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(0)
  state = ONLY_YOU // the app reached the workspace
  await clock.advance(2000)
  expect(await ui.find({ key: 'share' })).toBeDefined()
  await ui.unmount()
})

test('a refused Share says why and keeps the offer', async ($, on) => {
  let share: Reply | 'refuse' = { status: 404, body: { error: 'unknown_session', message: 'Lightbulb does not see this session yet. Wait a few seconds, then try again.' } }
  const { clock, socket } = world(on, path => (path === '/v1/share' ? share : ONLY_YOU))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'share' })
  expect(await ui.find({ type: 'Text', text: 'Lightbulb does not see this session yet. Wait a few seconds, then try again.' })).toBeDefined()
  expect(await ui.find({ key: 'share' })).toBeDefined()
  share = 'refuse'
  await ui.press({ key: 'share' })
  expect(await ui.find({ type: 'Text', text: 'Lightbulb did not answer. Open the Lightbulb app, then try again.' })).toBeDefined()
  socket.live = false // sharing was switched off for this Mac: the app's socket is gone
  await ui.press({ key: 'share' })
  expect(await ui.find({ type: 'Text', text: /^Lightbulb is not sharing on this Mac\./ })).toBeDefined()
  await ui.unmount()
})

test('every band Button has its own hotkey', async ($, on) => {
  let state: SessionState = asking
  const { clock } = world(on, () => ({ status: 200, body: state }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const hotkeys = async () => {
    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    const keys = Object.fromEntries((await ui.findAll({ type: 'Button' })).map(b => [b.key, b.props.hotkey]))
    await ui.unmount()
    return keys
  }
  expect(await hotkeys()).toEqual({ pause: 'p', allow: 'a', decline: 'd' })
  state = { ...base, paused: true }
  await clock.advance(2000)
  expect(await hotkeys()).toEqual({ resume: 's' }) // the same key as Share on a session that was never shared
})

// Prod 2026-10-07, Claude Code 2.1.292: `a` at the prompt typed "a"; only a click reached Allow.
// The engine presses a band Button's letter only while the band holds the keyboard, and it takes
// the keyboard on ctrl+x tab (abovePrompt:focus) or a click. So the band says so.
test('the key hint names the way to the band and the letters that work there', () => {
  expect(keysHint(asking)).toBe('ctrl+x tab, then a or d · or click')
  expect(keysHint(base)).toBe('ctrl+x tab, then p')
  expect(keysHint({ ...base, paused: true })).toBe('ctrl+x tab, then s')
})

test('the terminal band shows the key hint beside its buttons; a desktop, which has no chord, does not', async ($, on) => {
  let state: SessionState = asking
  const { clock } = world(on, () => ({ status: 200, body: state }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const hints = async (surface: (typeof SURFACES)[number]) => {
    const ui = await $.ui.mount({ ...BAND, surface })
    const found = (await ui.findAll({ type: 'Text', text: /ctrl\+x tab/ })).map(t => t.text.trim())
    await ui.unmount()
    return found
  }
  // One hint, on the row whose answer is waited for.
  expect(await hints('terminal')).toEqual(['ctrl+x tab, then a or d · or click'])
  expect(await hints('desktop')).toEqual([])
  state = base
  await clock.advance(2000)
  expect(await hints('terminal')).toEqual(['ctrl+x tab, then p'])
})

// A Button's `action` takes one of the engine's own keybinding actions and nothing else: any other
// name (a mod's own, a `command:` one) makes the engine refuse the whole band, clicks included.
// Borrowing an engine action (the diff panel's) would put Allow on a key that means something else.
test('no band Button names a keybinding action', async ($, on) => {
  const { clock } = world(on, () => ({ status: 200, body: asking }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const buttons = await ui.findAll({ type: 'Button' })
  expect(buttons.map(b => b.key)).toEqual(['pause', 'allow', 'decline'])
  expect(buttons.map(b => b.props.action)).toEqual([undefined, undefined, undefined])
  await ui.unmount()
})

test('a removed socket hides the band at once; a failing fetch on a live socket keeps it', async ($, on) => {
  let answer: Answer = () => ({ status: 200, body: asking })
  const { clock, socket } = world(on, (path, body) => answer(path, body))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const buttons = async () => {
    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
    const n = (await ui.findAll({ type: 'Button' })).length
    await ui.unmount()
    return n
  }
  expect(await buttons()).toBe(3)
  answer = () => 'refuse' // socket file still there: the app is busy
  await clock.advance(2000)
  expect(await buttons()).toBe(3)
  socket.live = false // sharing stopped: the app removed its socket
  await clock.advance(2000)
  expect(await buttons()).toBe(0)
})

test('a pick-up request names itself and Allow posts its request id', async ($, on) => {
  const pickup: SessionState = { ...base, joinRequests: [{ userId: 'u1', name: 'Mira Shah', requestId: 'r9', kind: 'pickup' }] }
  const { calls, clock } = world(on, path => ({ status: 200, body: path === '/v1/decide' ? base : pickup }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: SURFACES[0] })
  expect(await ui.find({ type: 'Text', text: 'Mira Shah wants to pick this up · conversation + uncommitted changes' })).toBeDefined()
  await ui.press({ key: 'allow' })
  const post = calls.filter(c => c.method === 'POST')
  expect(post[post.length - 1].body).toEqual({ decision: 'allow', userId: 'u1', requestId: 'r9' })
  await ui.unmount()
})

test("an agent's ask names its message and Allow posts the agent id and the ask", async ($, on) => {
  const ask: SessionState = { ...base, joinRequests: [{ userId: 'agent-friday', name: 'Friday', requestId: 'ask-1', kind: 'agent_message', action: 'send', text: 'Run the tests' }] }
  const { calls, clock } = world(on, path => ({ status: 200, body: path === '/v1/decide' ? base : ask }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: SURFACES[0] })
  expect(await ui.find({ type: 'Text', text: 'Friday wants to message this session: Run the tests' })).toBeDefined()
  await ui.press({ key: 'allow' })
  const post = calls.filter(c => c.method === 'POST')
  expect(post[post.length - 1].body).toEqual({ decision: 'allow', userId: 'agent-friday', requestId: 'ask-1' })
  await ui.unmount()
})

test('a continued marker becomes one line; junk becomes nothing', () => {
  const c = parseContinued('{"from":"Prasiddha","title":"Fix login"}')!
  expect(continuedLine(c)).toBe("Continued from Prasiddha's session · Share yours (thelightbulb.company)")
  expect(parseContinued('{"from":""}')).toBeNull()
  expect(parseContinued('not json')).toBeNull()
  expect(parseContinued('{"from":"A\\u001b[31mB"}')!.from).toBe('A[31mB')
  expect(parseContinued('{"from":"A\\u009b31mB"}')!.from).toBe('A31mB') // C1 too
})

test('an unshared continued session shows the Continued-from band', async ($, on) => {
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'fork-1' }))
  on('env.get', ($, e) => ({ value: e.name === 'HOME' ? '/Users/r' : undefined }))
  on('fs.read', ($, e) =>
    e.path === '/Users/r/.lightbulb/continued/fork-1.json'
      ? { value: '{"from":"Prasiddha","title":"Fix login"}' }
      : { deny: `no file ${e.path}` }) // no socket file: the app says nothing is shared
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ENGINE }))
  const clock = mock.clock(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /Continued from Prasiddha's session · Share yours/ })).toBeDefined()
  await ui.unmount()
})

// --- who typed in the live terminal -------------------------------------------

const ADEN = { userId: 'user_a', name: 'Aden Kim' }

test('typist reads who typed only from a live, unpaused session, as printable text', () => {
  expect(typist({ ...base, typedBy: ADEN })).toEqual({ kind: 'typed', name: 'Aden Kim', id: 'user_a' })
  expect(typist({ ...base, typedBy: { userId: 'user_a', name: ' Aden\u001b[31m Kim\n' } })).toEqual({ kind: 'typed', name: 'Aden[31m Kim', id: 'user_a' })
  expect(typist(base)).toBeNull()
  expect(typist({ ...base, terminal: null, typedBy: ADEN })).toBeNull() // a transcript takes no typing
  expect(typist({ ...base, paused: true, typedBy: ADEN })).toBeNull()
  expect(typist({ ...base, typedBy: { userId: 'user_a', name: ' ' } })).toBeNull()
  expect(typist(null)).toBeNull()
  expect(typist(undefined)).toBeNull()
})

test('typed prompts are remembered by text, the last submit wins, and the memory is bounded', () => {
  const rows = new Map<string, Sender>()
  const aden: Sender = { kind: 'typed', name: 'Aden Kim', id: 'user_a' }
  rememberTyped(rows, ' run the tests\n', aden)
  expect(rows.get('run the tests')).toEqual(aden)
  rememberTyped(rows, 'run the tests', null) // the owner submits the same text: no label
  expect(rows.size).toBe(0)
  rememberTyped(rows, '  ', aden)
  expect(rows.size).toBe(0)
  for (let i = 0; i < 250; i++) rememberTyped(rows, `p${i}`, aden)
  expect(rows.size).toBe(200)
  expect(rows.has('p0')).toBe(false)
  expect(rows.has('p249')).toBe(true)
})

const TYPED_ROW = (text: string, isExpanded: boolean, surface: 'terminal' | 'desktop') => ({
  plugin: P,
  surface,
  component: 'UserMessage' as const,
  props: { text, origin: { kind: 'composer' as const }, isExpanded },
})
const ENGINE_ROW = '(the engine draws its own row)'
// The engine beneath a typed prompt: it enters, and its row draws.
function prompts(on: On) {
  on('prompt.submit', ($, e) => ({ text: e.text, origin: e.origin }))
  on('ui.render', { component: 'UserMessage' }, ($, e) => $.ui.resolve(e).Text({ children: ENGINE_ROW }))
}
const typedPrompt = (text: string) => ({ text, wait: false, origin: { kind: 'composer' as const } })

test("a prompt a teammate typed in the live terminal is labelled theirs; the owner's own is not", async ($, on) => {
  let state: SessionState = base
  const { calls, clock } = world(on, () => ({ status: 200, body: state }))
  prompts(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  state = { ...base, typedBy: ADEN }
  const before = calls.length
  await $.prompt.submit(typedPrompt('run the tests'))
  expect(calls.slice(before)).toMatchObject([{ url: 'http://lightbulb/v1/state', method: 'GET' }]) // asked once, at the Enter
  for (const surface of SURFACES) {
    for (const isExpanded of [false, true]) {
      const ui = await $.ui.mount(TYPED_ROW('run the tests', isExpanded, surface))
      expect(await ui.find({ type: 'Text', text: 'Aden Kim · typed' })).toMatchObject({ props: { bold: true, color: colorFor('user_a') } })
      expect(await ui.find({ type: 'Box' })).toMatchObject({ props: { borderStyle: 'round', borderColor: colorFor('user_a') } })
      expect(await ui.find({ type: 'Text', text: 'run the tests' })).toBeDefined()
      await ui.unmount()
    }
  }
  // The owner typed the next one: the holder says so, the app sends no typedBy.
  state = base
  await $.prompt.submit(typedPrompt('my own prompt'))
  let ui = await $.ui.mount(TYPED_ROW('my own prompt', false, 'terminal'))
  expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE_ROW] })
  await ui.unmount()
  // The same text, now the owner's: the label goes (rows are told apart by text alone).
  await $.prompt.submit(typedPrompt('run the tests'))
  ui = await $.ui.mount(TYPED_ROW('run the tests', false, 'terminal'))
  expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE_ROW] })
  await ui.unmount()
})

test('a typed prompt asks the app nothing unless the session is shared live', async ($, on) => {
  let state: SessionState | null = null
  const { calls, clock } = world(on, () => (state ? { status: 200, body: state } : { status: 404, body: { error: 'not_shared' } }))
  prompts(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  for (const now of [null, { ...base, terminal: null, typedBy: ADEN }, { ...base, paused: true, typedBy: ADEN }]) {
    state = now
    await clock.advance(2000) // the poll reads it
    const before = calls.length
    await $.prompt.submit(typedPrompt('hello'))
    expect(calls.length).toBe(before)
    const ui = await $.ui.mount(TYPED_ROW('hello', false, 'terminal'))
    expect(await ui.drawn()).toEqual({ type: 'Text', children: [ENGINE_ROW] })
    await ui.unmount()
  }
})

test('a typed prompt does not wait on an app that stopped answering', async ($, on) => {
  let answer: Answer = () => ({ status: 200, body: base })
  const { clock } = world(on, (path, body) => answer(path, body))
  prompts(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  answer = () => 'hang'
  let entered = false
  const sent = $.prompt.submit(typedPrompt('still goes')).then(() => { entered = true })
  await clock.advance(1499)
  expect(entered).toBe(false)
  await clock.advance(1)
  await sent
  expect(entered).toBe(true)
})
