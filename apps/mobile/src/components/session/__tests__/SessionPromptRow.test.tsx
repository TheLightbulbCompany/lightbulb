import { fireEvent, render, screen } from "@testing-library/react-native";
import type { LocalSession, useSessionCommand } from "@lightbulb/shared";

import { SessionPromptRow } from "../SessionPromptRow";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

it("lets a verified queue session accept a follow-up while the Mac is offline", async () => {
  const session = {
    sessionId: "s1", provider: "claude", status: "offline", capabilitiesVerified: true,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, controlUnavailableReason: null,
  } as LocalSession;
  const send = jest.fn();
  const command = { send, locked: false, chip: null, attempt: null, canSteer: false, canInterrupt: false,
    canCancel: false, canCheck: false, checking: false } as unknown as ReturnType<typeof useSessionCommand>;
  await render(<SessionPromptRow session={session} text="Continue" onChangeText={jest.fn()} command={command} />);
  expect(screen.getByLabelText("Send a follow-up to Claude")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Send follow-up"));
  expect(send).toHaveBeenCalledWith("Continue");
});

it("retries a rejected replacement through its preserved request lineage", async () => {
  const session = { sessionId: "s1", provider: "claude", status: "idle", capabilitiesVerified: true,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, controlUnavailableReason: null } as LocalSession;
  const send = jest.fn();
  const retry = jest.fn();
  const command = { send, retry, locked: false, canSend: true, chip: null, checking: false,
    canSteer: false, canInterrupt: false, canCancel: false, canCheck: false,
    attempt: { request: { requestId: "new", expectedGeneration: "g1", kind: "send", text: "Continue" },
      replacesRequestId: "old", state: "received", receipt: { state: "rejected", cancelledAt: null } },
  } as unknown as ReturnType<typeof useSessionCommand>;
  await render(<SessionPromptRow session={session} text="" onChangeText={jest.fn()} command={command} />);
  await fireEvent.press(screen.getByLabelText("Retry follow-up"));
  expect(retry).toHaveBeenCalledTimes(1);
  expect(send).not.toHaveBeenCalled();
});
