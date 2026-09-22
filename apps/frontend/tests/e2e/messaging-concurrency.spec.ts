import { test, expect } from '@playwright/test';
import { clerkSetup, setupClerkTestingToken } from '@clerk/testing/playwright';
import crypto from 'crypto';
import { createE2EUser, cleanupUser, type E2EUser } from './fixtures/user';
import { AuthedFetch } from './fixtures/api';
import { DDBReader } from './fixtures/ddb-reader';
import { signIn } from './drivers/sign-in';
import { onboardWorkspace } from './drivers/onboarding';
import { containerHealthy } from './assertions/container';

// Concurrency harness (@concurrency): does talking to the same / different agents in
// different chats reply correctly, and how much do concurrent turns slow each other?
// Runs against a live dev workspace (real runtime + LLM spend), so it is on-demand only:
//   BASE_URL=https://dev.flickmeon.ai npx playwright test messaging-concurrency
// Correctness is asserted; timings are recorded (attached as JSON), not gated yet.
// Default: a fresh E2E user + workspace, cleaned up after. Its free tier draws on the
// OpenRouter free-model budget prod trial users share (8 turns, ~25 calls), so prefer:
// EXISTING_USER_ID + EXISTING_ORG_ID -- run inside an existing paid workspace instead (the
// owner's, with their go-ahead). Only two new channels are created and nothing is cleaned up.

type Member = { kind: string; id: string; displayName: string; mentionHandle: string | null };
type Room = { roomId: string; members: Member[]; activeRuns?: { agentId: string; runId: string }[] };
type Message = { messageSeq: number; author: { kind: string; id: string }; text: string };
type Timing = { scenario: string; room: string; agent: string; tag: string; replyMs: number; idleMs: number; overlapped?: boolean };

const REPLY_TIMEOUT_MS = 6 * 60_000;
const POLL_MS = 500;

test.describe('E2E: messaging concurrency @concurrency', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(40 * 60_000);

  let user: E2EUser;
  let ceo: Member;
  let other: Member;
  let roomA: string;
  let roomB: string;
  const timings: Timing[] = [];
  const tagsByRoom = new Map<string, Set<string>>();

  const api = () => user.api;

  const existingUserId = process.env.EXISTING_USER_ID;
  const existingOrgId = process.env.EXISTING_ORG_ID;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    if (existingUserId && existingOrgId) {
      // Clerk testing tokens exist only on development instances; an existing (possibly
      // production) account signs in with the admin-issued ticket alone.
      const runId = `${Date.now()}-${crypto.randomBytes(3).toString('hex')}`;
      const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000/api/v1';
      const api = new AuthedFetch(page, apiUrl, runId);
      user = { runId, email: '', password: '', clerkUserId: existingUserId, orgId: existingOrgId, page, api, ddb: new DDBReader(api, existingUserId) };
    } else {
      await clerkSetup();
      await setupClerkTestingToken({ page });
      user = await createE2EUser(page);
    }
    console.log(`[concurrency] runId=${user.runId}`);
  });

  test.afterAll(async ({}, testInfo) => {
    await testInfo.attach('timings.json', { body: JSON.stringify(timings, null, 2), contentType: 'application/json' });
    console.table(timings);
    if (user && existingUserId) {
      // An existing workspace keeps its data; the harness only steps out of its own channels.
      for (const roomId of [roomA, roomB].filter(Boolean)) {
        await api().post(`/messaging/rooms/${roomId}/leave`, {}).catch((e) => console.warn(`[concurrency] leave ${roomId}: ${e}`));
      }
    } else if (user && !process.env.KEEP_WORKSPACE) await cleanupUser(user);
  });

  async function createChannel(name: string): Promise<string> {
    const room = await api().post<Room>('/messaging/rooms', {
      clientRoomId: crypto.randomUUID(),
      name,
      kind: 'channel',
      // Mentions, not active: each turn wakes exactly the agent the harness addresses.
      members: [ceo, other].map((m) => ({ kind: 'agent', id: m.id, role: 'member', listenMode: 'mentions' })),
    });
    return room.roomId;
  }

  /** Send one addressed message and wait until the agent has replied and its run is gone. */
  async function turn(scenario: string, roomId: string, agent: Member, t0 = Date.now()): Promise<Timing> {
    const tag = `T${crypto.randomBytes(3).toString('hex')}`;
    if (!tagsByRoom.has(roomId)) tagsByRoom.set(roomId, new Set());
    tagsByRoom.get(roomId)!.add(tag);
    const sent = await api().post<Message>(`/messaging/rooms/${roomId}/messages`, {
      clientMessageId: crypto.randomUUID(),
      text: `@${agent.mentionHandle} Reply with exactly the token ${tag} and nothing else. Do not use tools.`,
      mentions: [{ kind: 'agent', id: agent.id }],
      replyToMessageId: null,
      threadRootMessageId: null,
    });
    let replyMs = 0;
    while (Date.now() - t0 < REPLY_TIMEOUT_MS) {
      if (!replyMs) {
        const page = await api().get<{ messages: Message[] }>(
          `/messaging/rooms/${roomId}/messages?afterSeq=${sent.messageSeq}&limit=100`,
        );
        if (page.messages.some((m) => m.author.id === agent.id && m.text.includes(tag))) replyMs = Date.now() - t0;
      }
      if (replyMs) {
        const room = await api().get<Room>(`/messaging/rooms/${roomId}`);
        if (!(room.activeRuns ?? []).some((r) => r.agentId === agent.id)) {
          return { scenario, room: roomId.slice(0, 8), agent: agent.displayName, tag, replyMs, idleMs: Date.now() - t0 };
        }
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
    throw new Error(`${scenario}: no reply carrying ${tag} from ${agent.displayName} within ${REPLY_TIMEOUT_MS}ms`);
  }

  /** Two turns started together; records whether their reply windows overlapped. */
  async function pair(scenario: string, a: [string, Member], b: [string, Member]) {
    const t0 = Date.now();
    const [x, y] = await Promise.all([turn(scenario, a[0], a[1], t0), turn(scenario, b[0], b[1], t0)]);
    // Overlapped = the later reply landed before the earlier run ended (within one poll).
    // Serialized turns (the per-agent lock) read false.
    const overlapped = Math.max(x.replyMs, y.replyMs) < Math.min(x.idleMs, y.idleMs) + POLL_MS;
    for (const r of [x, y]) timings.push({ ...r, overlapped });
  }

  test('setup: workspace ready and two channels with two agents', async () => {
    if (!process.env.BASE_URL) throw new Error('BASE_URL is required');
    await signIn(user.page, process.env.BASE_URL, user.clerkUserId);
    if (existingOrgId) {
      await user.page.evaluate(async (organization) => {
        await (window as unknown as { Clerk: { setActive: (o: { organization: string }) => Promise<void> } })
          .Clerk.setActive({ organization });
      }, existingOrgId);
    } else {
      await onboardWorkspace(user.page, `track-f-concurrency-${user.runId}`, (orgId) => { user.orgId = orgId; });
    }
    await containerHealthy(user.api, { timeoutMs: 15 * 60_000 });

    const { rooms } = await api().get<{ rooms: (Room & { name: string | null; kind: string })[] }>('/messaging/rooms');
    // The workspace's live roster (a room can still list a deleted agent, or a shared vendor
    // channel another workspace's), narrowed to agents the caller can already @mention.
    const roster = await api().post<{ ok?: boolean; payload?: { agents?: { id: string }[] } }>(
      '/runtime/rpc', { requestId: crypto.randomUUID(), method: 'agents.list', params: {} },
    );
    const live = new Set((roster.payload?.agents ?? []).map((a) => a.id));
    const agents = [...new Map(rooms.flatMap((r) => r.members)
      .filter((m) => m.kind === 'agent' && m.mentionHandle && live.has(m.id)).map((m) => [m.id, m])).values()];
    expect(agents.length, 'at least two agents to address').toBeGreaterThanOrEqual(2);
    ceo = agents.find((m) => /ceo/i.test(m.displayName)) ?? agents[0];
    other = agents.find((m) => m.id !== ceo.id)!;
    roomA = await createChannel(`track-f-a-${user.runId.slice(-6)}`);
    roomB = await createChannel(`track-f-b-${user.runId.slice(-6)}`);
  });

  test('cold start: first turn', async () => {
    timings.push(await turn('cold-first', roomA, ceo));
  });

  test('baseline: single warm turn', async () => {
    timings.push(await turn('warm-single', roomA, ceo));
  });

  test('a: same agent, two rooms, concurrently', async () => {
    await pair('same-agent-two-rooms', [roomA, ceo], [roomB, ceo]);
  });

  test('b: two agents, two rooms, concurrently', async () => {
    await pair('two-agents-two-rooms', [roomA, ceo], [roomB, other]);
  });

  test('c: same agent, same room, back-to-back', async () => {
    await pair('same-room-back-to-back', [roomA, ceo], [roomA, ceo]);
  });

  test('correctness: no reply leaked into the other room', async () => {
    for (const [roomId, own] of tagsByRoom) {
      const { messages } = await api().get<{ messages: Message[] }>(`/messaging/rooms/${roomId}/messages?afterSeq=0&limit=100`);
      const foreign = [...tagsByRoom].filter(([id]) => id !== roomId).flatMap(([, tags]) => [...tags]);
      const leaked = messages.filter((m) => m.author.kind === 'agent' && foreign.some((t) => m.text.includes(t)));
      expect(leaked.map((m) => m.text), `cross-talk into ${roomId}`).toEqual([]);
      expect(own.size).toBeGreaterThan(0);
    }
  });
});
