import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
const mockDownload = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, downloadBlob: mockDownload }),
}));

import { TeamInbox } from "../TeamInbox";

const ADDR = "agents@acme.thelightbulb.company";
const MEETING = "11111111-1111-4111-8111-111111111111";

const page = {
  address: ADDR,
  meetings: [
    {
      id: MEETING,
      title: "Weekly sync",
      organizer: "pat@acme.com",
      status: "scheduled",
      start_at: 1791158400,
      end_at: 1791160200,
      repeats: true,
      transcripts: [{ start_at: 1790553600 }],
    },
  ],
  messages: [
    {
      key: "k2",
      direction: "in",
      from: "pat@acme.com",
      to: ADDR,
      subject: "Quarterly numbers",
      at: "2026-10-02T10:00:00+00:00",
      agent_id: "a1",
      text: "Here are the numbers.",
    },
    {
      key: "k1",
      direction: "out",
      from: ADDR,
      to: "vendor@x.y",
      subject: "Invoice question",
      at: "2026-10-01T10:00:00+00:00",
      agent_id: "a2",
      text: "",
    },
  ],
  next: "k1",
};

function renderInbox() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <TeamInbox agents={[{ id: "a1", name: "Finance" } as never]} />
    </SWRConfig>,
  );
}

describe("TeamInbox", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockDownload.mockReset();
  });

  it("renders meetings and mail in both directions", async () => {
    mockGet.mockResolvedValue(page);
    renderInbox();

    expect(await screen.findByText("Weekly sync")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/agent-mail/inbox");
    expect(screen.getByText(/^Repeats · Next .* · Agents will join$/)).toBeInTheDocument();
    expect(screen.getByText("pat@acme.com")).toBeInTheDocument();
    expect(screen.getByText("vendor@x.y")).toBeInTheDocument();
    expect(screen.getByText("Finance")).toBeInTheDocument();
    expect(screen.getByLabelText("Received")).toBeInTheDocument();
    expect(screen.getByLabelText("Sent")).toBeInTheDocument();

    // An inbound row expands to its text.
    expect(screen.queryByText("Here are the numbers.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /quarterly numbers/i }));
    expect(screen.getByText("Here are the numbers.")).toBeInTheDocument();

    // Load more asks for the page before the cursor.
    mockGet.mockImplementation((url: string) =>
      Promise.resolve(url === "/agent-mail/inbox" ? page : { address: ADDR, messages: [], next: null }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledWith("/agent-mail/inbox?before=k1"));
  });

  it("fetches a transcript on click and hides it on the second click", async () => {
    mockGet.mockResolvedValue(page);
    mockDownload.mockResolvedValue({ text: async () => "[20:23] Pat: Hi all\n" });
    renderInbox();

    const button = await screen.findByRole("button", { name: /^Transcript · / });
    fireEvent.click(button);
    expect(await screen.findByText(/Pat: Hi all/)).toBeInTheDocument();
    expect(mockDownload).toHaveBeenCalledWith(
      `/agent-mail/meetings/${MEETING}/transcript?start_at=1790553600`,
    );
    fireEvent.click(button);
    expect(screen.queryByText(/Pat: Hi all/)).toBeNull();
  });

  it("says so when a transcript fails to load", async () => {
    mockGet.mockResolvedValue(page);
    mockDownload.mockRejectedValue(new Error("404"));
    renderInbox();

    fireEvent.click(await screen.findByRole("button", { name: /^Transcript · / }));
    expect(await screen.findByText("Couldn't load the transcript.")).toBeInTheDocument();
  });

  it("shows the empty state when nothing has arrived", async () => {
    mockGet.mockResolvedValue({ address: ADDR, meetings: [], messages: [], next: null });
    renderInbox();

    expect(await screen.findByText(/Nothing has arrived yet/)).toBeInTheDocument();
    expect(screen.queryByText("Meetings")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });
});
