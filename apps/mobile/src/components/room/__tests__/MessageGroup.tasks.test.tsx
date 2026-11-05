import { fireEvent, render, screen } from "@testing-library/react-native";
import type { CanonicalRoomMessage, RoomMember } from "@lightbulb/shared";
import { FIXTURES, LIGHTBULB, SAM } from "@lightbulb/shared/tasks/fixtures";

const live = { superAgent: true, tasks: null as unknown };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "00000000-0000-4000-8000-000000000000" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));

import { MessageGroup } from "../MessageGroup";

const Fresh = ({ children }: { children: React.ReactNode }) => <>{children}</>;
const attachmentUrl = jest.fn(async () => ({ url: "https://s3.example/x", expiresIn: 900 }));
const members: RoomMember[] = [
  { kind: "human", id: SAM, displayName: "Sam", role: "owner", listenMode: null, joinedAt: "2026-11-04T00:00:00Z", joinedSeq: 0, mentionHandle: null },
  { kind: "agent", id: LIGHTBULB, displayName: "Lightbulb", role: "member", listenMode: "active", joinedAt: "2026-11-04T00:00:00Z", joinedSeq: 0, mentionHandle: "lightbulb" },
] as RoomMember[];
const [ask, reply, result] = FIXTURES.step07.messages;
const list = jest.fn(async () => ({ tasks: FIXTURES.step02.tasks, next: null }));
const group = (message: CanonicalRoomMessage, over: Partial<Parameters<typeof MessageGroup>[0]> = {}) =>
  render(<Fresh><MessageGroup message={message} collapsed={false} replyCount={0} currentUserId={SAM} attachmentUrl={attachmentUrl} members={members} {...over} /></Fresh>);

beforeEach(() => { live.superAgent = true; list.mockClear(); live.tasks = { cacheScope: `org_group_${Math.random()}`, list, questions: jest.fn(async () => ({ items: [] })) }; });

it("Lightbulb · Finance byline on a result line", async () => {
  await group(result);
  // The byline, and the fees card under it, both read "Lightbulb · Finance"; the author's own name is gone.
  expect(screen.getAllByText("Lightbulb · Finance").length).toBeGreaterThanOrEqual(1);
  expect(screen.queryByText("Lightbulb")).toBeNull();
});

it("started and suggested refs split under the reply", async () => {
  await group(reply);
  expect(await screen.findByText("Workshop signup app")).toBeTruthy();
  expect(screen.getByText("Launch email draft")).toBeTruthy();
  expect(screen.getByText("SUGGESTED")).toBeTruthy();
  expect(screen.getByText("Stripe fee estimate")).toBeTruthy();
  expect(screen.getByText("would use a paid tool")).toBeTruthy();
  expect(list).toHaveBeenCalledTimes(1);
});

it("the person's message says where it went; the agent's 👀 is a receipt, not a toggle", async () => {
  const onToggleReaction = jest.fn();
  await group({ ...ask, reactions: { "👀": [LIGHTBULB] } }, { onToggleReaction });
  expect(screen.getByText("Sent to 2 threads, 1 suggested ›")).toBeTruthy();
  const chip = screen.getByLabelText("Lightbulb saw this");
  fireEvent.press(chip);
  expect(onToggleReaction).not.toHaveBeenCalled();
});

it("flag off: no cards, no byline change, no /tasks read", async () => {
  live.superAgent = false;
  await group(reply);
  await group(result);
  expect(screen.queryByText("Workshop signup app")).toBeNull();
  expect(screen.queryByText("Lightbulb · Finance")).toBeNull();
  expect(list).not.toHaveBeenCalled();
});
