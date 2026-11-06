import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import type { RoomDetail } from "@lightbulb/shared";
import type { AppRow } from "@/hooks/useApps";

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "user_alice" }) }));
const rooms = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock("@/hooks/useRooms", () => ({ useRooms: () => rooms.value }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const openConversation = vi.fn(async () => ({ roomId: "conv-1" }));
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ openConversation }) }));
const setSideRoom = vi.fn();
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({ useMessagingWorkspaceOptional: () => ({ setSideRoom }) }));
beforeAll(() => import("@/components/messaging/openui/OpenUICard"));

import { AppConversation } from "../AppConversation";
import { typeInComposer } from "@/test-utils/composer";

const app = { app_id: "a1", name: "Invoices", room_id: null, state: "building", open_to: "workspace", budget_usd: 20, preview_url: null,
  live_url: null, live_version: null, last_opened_at: null, spend_month_cents: 0, can_open: true } as AppRow;
const detail = { roomId: "conv-1", name: "Invoices", kind: "channel", members: [] } as unknown as RoomDetail;
const selectRoom = vi.fn();
const sendMessage = vi.fn(async () => {});
beforeEach(() => {
  selectRoom.mockClear(); sendMessage.mockClear(); openConversation.mockClear(); setSideRoom.mockClear();
  rooms.value = {
    selectRoom, sendMessage, activeRoomId: "conv-1", activeRoom: detail, messages: [
      { messageId: "m1", roomId: "conv-1", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "ag", displayName: "Builder" },
        text: "The first version is up.", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-11-04T10:00:00Z", runId: null },
    ],
    optimisticMessages: [], historyComplete: true, historyLoading: false, historyErrorMessage: null,
    loadOlder: vi.fn(), sendState: () => "idle", reconcileSend: vi.fn(), httpReady: true,
  };
});

it("opens the app's conversation through its door, shows it, and registers it as on screen", async () => {
  const view = render(<AppConversation app={app} />);
  await waitFor(() => expect(selectRoom).toHaveBeenCalledWith("conv-1"));
  expect(openConversation).toHaveBeenCalledExactlyOnceWith("a1");
  await waitFor(() => expect(setSideRoom).toHaveBeenLastCalledWith("app-conversation:a1", "conv-1")); // not toasted while on screen
  expect(screen.getByRole("complementary", { name: "Conversation" })).toBeInTheDocument();
  expect(screen.getByText("The first version is up.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Close chat" })).toBeNull(); // it stays beside the preview
  view.unmount();
  expect(setSideRoom).toHaveBeenLastCalledWith("app-conversation:a1", null);
});

it("a hidden conversation (narrow panel showing the app) is not on screen, so it still notifies", async () => {
  const view = render(<AppConversation app={app} shown={false} />);
  await waitFor(() => expect(selectRoom).toHaveBeenCalledWith("conv-1"));
  expect(setSideRoom).not.toHaveBeenCalledWith("app-conversation:a1", "conv-1");
  view.rerender(<AppConversation app={app} shown />);
  expect(setSideRoom).toHaveBeenLastCalledWith("app-conversation:a1", "conv-1");
  view.rerender(<AppConversation app={app} shown={false} />);
  expect(setSideRoom).toHaveBeenLastCalledWith("app-conversation:a1", null);
});

it("sends into the conversation room like any room message", async () => {
  render(<AppConversation app={app} />);
  const box = await screen.findByLabelText("Tell the agent what to change");
  await waitFor(() => expect(box).toHaveAttribute("contenteditable", "true"));
  typeInComposer("make the header blue", box);
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
  expect(sendMessage).toHaveBeenCalledWith(expect.objectContaining({ text: "make the header blue" }));
});
