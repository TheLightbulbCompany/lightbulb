import { fireEvent, render, waitFor } from "@testing-library/react-native";
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
    const view = render(<CatchUpSummary messages={messages} readSeq={1} offline={false} />);

    fireEvent.press(view.getByLabelText("Summary"));
    expect(view.getByText("Summarizing…")).toBeTruthy();
    await waitFor(() => expect(view.getByText("Main discussion")).toBeTruthy());
    expect(view.getByText("Decisions")).toBeTruthy();
    expect(view.getByText("Action items")).toBeTruthy();
    expect(view.getByText("Open questions")).toBeTruthy();
    expect(view.getAllByText("Friday: We agreed to ship today. Who will verify it?").length).toBeGreaterThan(0);

    fireEvent.press(view.getByLabelText("Dismiss summary"));
    expect(view.queryByLabelText("Conversation summary")).toBeNull();
    expect(view.getByLabelText("Summary")).toBeTruthy();
  });

  it("shows a recoverable empty state", async () => {
    const view = render(<CatchUpSummary messages={messages} readSeq={2} offline={false} />);
    fireEvent.press(view.getByLabelText("Summary"));

    await waitFor(() => expect(view.getByText("You’re caught up — there are no new messages to summarize.")).toBeTruthy());
  });

  it("labels offline scope and retries a generation failure", async () => {
    const generate = jest.fn<
      ReturnType<typeof buildCatchUpSummary>,
      Parameters<typeof buildCatchUpSummary>
    >()
      .mockImplementationOnce(() => { throw new Error("generation failed"); })
      .mockImplementation(buildCatchUpSummary);
    const view = render(
      <CatchUpSummary
        messages={messages}
        readSeq={1}
        offline
        generateSummary={generate}
      />,
    );

    fireEvent.press(view.getByLabelText("Summary"));
    await waitFor(() => expect(view.getByText("Couldn’t generate the summary.")).toBeTruthy());
    expect(view.getByText("Offline · using messages already loaded on this device.")).toBeTruthy();

    fireEvent.press(view.getByLabelText("Retry summary"));
    await waitFor(() => expect(view.getByText("Main discussion")).toBeTruthy());
    expect(generate).toHaveBeenCalledTimes(2);
  });
});
