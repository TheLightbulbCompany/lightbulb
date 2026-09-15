import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { CanonicalRoomMessage } from "@lightbulb/shared";

import { CatchUpSummary } from "../CatchUpSummary";
import { buildCatchUpSummary } from "../../../lib/catchUpSummary";

const messages: CanonicalRoomMessage[] = [{
  messageId: "m-2",
  roomId: "room-1",
  messageSeq: 2,
  roomRevision: 2,
  author: { kind: "agent", id: "agent-1", displayName: "Friday" },
  text: "We agreed to ship today. Who will verify it?",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-15T02:54:00Z",
  runId: null,
}];

describe("CatchUpSummary", () => {
  it("keeps a clearly labeled action in chat and renders a dismissible summary", async () => {
    render(<CatchUpSummary messages={messages} readSeq={1} offline={false} />);

    fireEvent.press(screen.getByLabelText("Summary"));
    expect(screen.getByText("Summarizing…")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("Main discussion")).toBeTruthy());
    expect(screen.getByText("Decisions")).toBeTruthy();
    expect(screen.getByText("Action items")).toBeTruthy();
    expect(screen.getByText("Open questions")).toBeTruthy();
    expect(screen.getAllByText("Friday: We agreed to ship today. Who will verify it?").length).toBeGreaterThan(0);

    fireEvent.press(screen.getByLabelText("Dismiss summary"));
    expect(screen.queryByLabelText("Conversation summary")).toBeNull();
    expect(screen.getByLabelText("Summary")).toBeTruthy();
  });

  it("shows a recoverable empty state", async () => {
    render(<CatchUpSummary messages={messages} readSeq={2} offline={false} />);
    fireEvent.press(screen.getByLabelText("Summary"));

    await waitFor(() => expect(screen.getByText("You’re caught up — there are no new messages to summarize.")).toBeTruthy());
  });

  it("labels offline scope and retries a generation failure", async () => {
    const generate = jest.fn<typeof buildCatchUpSummary>()
      .mockImplementationOnce(() => { throw new Error("generation failed"); })
      .mockImplementation(buildCatchUpSummary);
    render(
      <CatchUpSummary
        messages={messages}
        readSeq={1}
        offline
        generateSummary={generate}
      />,
    );

    fireEvent.press(screen.getByLabelText("Summary"));
    await waitFor(() => expect(screen.getByText("Couldn’t generate the summary.")).toBeTruthy());
    expect(screen.getByText("Offline · using messages already loaded on this device.")).toBeTruthy();

    fireEvent.press(screen.getByLabelText("Retry summary"));
    await waitFor(() => expect(screen.getByText("Main discussion")).toBeTruthy());
    expect(generate).toHaveBeenCalledTimes(2);
  });
});
