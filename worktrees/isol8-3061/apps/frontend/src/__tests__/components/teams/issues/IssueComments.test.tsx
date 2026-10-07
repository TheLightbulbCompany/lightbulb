import { describe, test, expect, vi } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

// A failed post surfaces as a sonner toast rather than an inline alert (#898),
// and no <Toaster /> is mounted in jsdom — so spy on the notify call. Spread
// the real module so the rest of the tree keeps its other notify helpers.
const mockNotifyApiError = vi.fn();
vi.mock("@/lib/notify", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/notify")>()),
  notifyApiError: (err: unknown, fallback?: string) => mockNotifyApiError(err, fallback),
}));

import { IssueComments } from "@/components/teams/issues/IssueComments";
import type { IssueComment } from "@/components/teams/shared/types";

const baseComment = (overrides: Partial<IssueComment> = {}): IssueComment => ({
  id: "c1",
  body: "Hello world",
  createdAt: "2026-05-05T12:00:00Z",
  ...overrides,
});

describe("IssueComments", () => {
  test("renders empty state when no comments", () => {
    render(<IssueComments comments={[]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText(/no comments yet/i)).toBeInTheDocument();
  });

  test("renders loading state when isLoading + empty", () => {
    render(<IssueComments comments={[]} agents={[]} isLoading onSubmit={vi.fn()} />);
    expect(screen.getByText(/loading comments/i)).toBeInTheDocument();
  });

  test("renders list of comments with body", () => {
    const c1 = baseComment({ id: "c1", body: "First" });
    const c2 = baseComment({ id: "c2", body: "Second" });
    render(<IssueComments comments={[c1, c2]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
  });

  test("submit button disabled when body empty", () => {
    render(<IssueComments comments={[]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByRole("button", { name: /comment/i })).toBeDisabled();
  });

  test("submit fires onSubmit with trimmed body and clears textarea", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<IssueComments comments={[]} agents={[]} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText(/add a comment/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "  Hi there  " } });
    fireEvent.click(screen.getByRole("button", { name: /comment/i }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("Hi there"));
    await waitFor(() => expect(textarea.value).toBe(""));
  });

  test("Cmd+Enter inside textarea triggers submit", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<IssueComments comments={[]} agents={[]} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText(/add a comment/i);
    fireEvent.change(textarea, { target: { value: "via shortcut" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("via shortcut"));
  });

  test("Ctrl+Enter inside textarea triggers submit", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<IssueComments comments={[]} agents={[]} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText(/add a comment/i);
    fireEvent.change(textarea, { target: { value: "windows linux" } });
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("windows linux"));
  });

  test("plain Enter (no modifier) does NOT submit (allows newline insertion)", () => {
    const onSubmit = vi.fn();
    render(<IssueComments comments={[]} agents={[]} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText(/add a comment/i);
    fireEvent.change(textarea, { target: { value: "test" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  test("error from onSubmit is surfaced", async () => {
    mockNotifyApiError.mockClear();
    const failure = new Error("server boom");
    const onSubmit = vi.fn().mockRejectedValue(failure);
    render(<IssueComments comments={[]} agents={[]} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText(/add a comment/i);
    fireEvent.change(textarea, { target: { value: "test" } });
    fireEvent.click(screen.getByRole("button", { name: /comment/i }));
    await waitFor(() =>
      expect(mockNotifyApiError).toHaveBeenCalledWith(failure, "Failed to post comment"),
    );
  });

  test("renders comment body as markdown", () => {
    const c = baseComment({ id: "cm", body: "## Heading text\n\nbody para" });
    render(<IssueComments comments={[c]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText("Heading text")).toBeInTheDocument();
    expect(screen.queryByText("## Heading text")).toBeNull();
  });

  test("resolves agent author name from the agents prop", () => {
    const c = baseComment({ id: "ca", body: "hi", authorAgentId: "ag1", authorKind: "agent" });
    render(
      <IssueComments
        comments={[c]}
        agents={[{ id: "ag1", name: "Atlas" } as never]}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    expect(screen.queryByText("Agent")).toBeNull();
  });

  test("shows a relative timestamp for a past comment", () => {
    const c = baseComment({ id: "ct", body: "hi", createdAt: "2020-01-01T00:00:00Z" });
    render(<IssueComments comments={[c]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText(/ago/i)).toBeInTheDocument();
  });
});
