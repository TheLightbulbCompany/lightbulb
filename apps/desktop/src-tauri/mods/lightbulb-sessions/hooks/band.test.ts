import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'
import { bandLine } from './state'
import type { SessionState } from '../types'

const P = 'lightbulb-sessions'
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
type Answer = (path: string, body: unknown) => { status: number; body: unknown } | 'refuse' | 'hang'
function world(on: On, answer: Answer, socketFile = true) {
  const calls: Call[] = []
  const clock = mock.clock(on)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('fs.read', ($, e) => {
    if (socketFile && e.path.endsWith('/lightbulb-sessions/socket')) return { value: `${SOCKET}\n` }
    return { deny: `no file ${e.path}` }
  })
  on('http.fetch', ($, e) => {
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    calls.push({ url: e.url, method: e.init?.method ?? 'GET', body, socketPath: e.init?.socketPath })
    const a = answer(new URL(e.url).pathname, body)
    if (a === 'refuse') return { deny: 'connect ECONNREFUSED' }
    if (a === 'hang') return new Promise<never>(() => {}) // accepts the connection, never answers
    const text = JSON.stringify(a.body)
    return { value: { status: a.status, ok: a.status >= 200 && a.status < 300, headers: {}, text } }
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return Text({ children: ENGINE })
  })
  return { calls, clock }
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
})

test('band says transcript for a plain session, and paused wins', () => {
  expect(bandLine({ ...base, terminal: null })).toBe('● Shared as a transcript')
  expect(bandLine({ ...base, paused: true })).toBe('○ Sharing paused')
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

const asking: SessionState = { ...base, joinRequests: [{ userId: 'u1', name: 'Aden Kim', requestId: 'r1' }] }

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

test('a refused decision shows the error line and keeps the request', async ($, on) => {
  const { clock } = world(on, path => path === '/v1/decide'
    ? { status: 409, body: { error: 'not_pending', message: 'gone' } }
    : { status: 200, body: asking })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    await ui.press({ key: 'allow' })
    expect(await ui.find({ type: 'Text', text: 'Lightbulb did not take that. Try again in the app.' })).toBeDefined()
    expect(await ui.find({ key: 'allow' })).toBeDefined()
    await ui.unmount()
  }
})

test('Pause posts pause, and a paused band offers Resume', async ($, on) => {
  const { calls, clock } = world(on, path => ({ status: 200, body: path === '/v1/decide' ? { ...base, paused: true } : base }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'pause' })
  expect(calls[calls.length - 1]?.body).toEqual({ decision: 'pause' })
  expect(await ui.find({ text: '○ Sharing paused' })).toBeDefined()
  expect(await ui.find({ key: 'resume' })).toBeDefined()
})
