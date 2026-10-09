import { fireEvent, render, screen } from "@testing-library/react-native";

import { DocumentCards } from "../DocumentCards";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
// SWR never fetches under jest-expo; the hook's reads are covered in packages/shared.
const mockPreview = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({}),
  useDocumentPreview: (_api: unknown, link: unknown) => mockPreview(link),
}));

const PREVIEW = {
  issue: { id: "uuid-12", identifier: "HAB-12", title: "Habits 365 research", status: "done" },
  document: {
    id: "doc-1",
    key: "research",
    title: "Habits 365: research plan",
    body: "# Habits 365: research plan\n\nHabit-tracking app, strongest at streak reminders.",
    updatedAt: new Date().toISOString(),
  },
};

describe("DocumentCards", () => {
  beforeEach(() => mockPreview.mockReset());

  it("renders a card for a document link and opens the document in a sheet", async () => {
    mockPreview.mockReturnValue(PREVIEW);
    await render(<DocumentCards text="Done: [plan](/HAB/issues/HAB-12#document-research)" />);
    expect(mockPreview).toHaveBeenCalledWith({ issueId: "HAB-12", key: "research" });
    const card = screen.getByTestId("document-card");
    expect(screen.getByText("HAB-12 · Habits 365 research")).toBeTruthy();
    expect(screen.getByText(/strongest at streak reminders/)).toBeTruthy();
    await fireEvent.press(card);
    // The sheet shows the full body, rendered as markdown.
    expect(screen.getAllByText(/Habits 365: research plan/).length).toBeGreaterThan(1);
  });

  it("renders nothing for plain text or a failed read", async () => {
    mockPreview.mockReturnValue(undefined);
    await render(<DocumentCards text="see /HAB/issues/HAB-12#document-plan and https://a.com" />);
    expect(screen.queryByTestId("document-card")).toBeNull();
    mockPreview.mockClear();
    await render(<DocumentCards text="no links here" />);
    expect(mockPreview).not.toHaveBeenCalled();
  });
});
