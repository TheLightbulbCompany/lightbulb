import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@lightbulb/shared";

vi.mock("@/components/teams/shared/components/MarkdownEditor", () => ({
  MarkdownEditor: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <textarea aria-label="Body" value={value} onChange={(e) => onChange(e.target.value)} />
  ),
}));
const put = vi.fn();
const mutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ put, mutate, read: () => ({ data: undefined }) }) }));
import { DocumentEditor } from "../DocumentEditor";

const doc = { id: "d1", key: "plan", title: "Launch plan", body: "Old body", latestRevisionId: "r3", latestRevisionNumber: 3 };

beforeEach(() => { put.mockReset(); mutate.mockReset(); });

it("saves a new revision against the revision it opened", async () => {
  put.mockResolvedValue({ ...doc, body: "New body", latestRevisionId: "r4", latestRevisionNumber: 4 });
  const onSaved = vi.fn();
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={onSaved} onCancel={vi.fn()} />);
  const body = await screen.findByRole("textbox", { name: "Body" });
  await userEvent.clear(body);
  await userEvent.type(body, "New body");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(put).toHaveBeenCalledWith("/issues/i1/documents/plan", { title: "Launch plan", format: "markdown", body: "New body", changeSummary: "Edited by Ada", baseRevisionId: "r3" });
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ latestRevisionId: "r4" })));
  expect(mutate).toHaveBeenCalledWith("/teams/issues/i1/documents/plan");
  expect(mutate).toHaveBeenCalledWith("/teams/issues/i1/documents");
});

it("a stale save keeps the draft and names the other editor", async () => {
  // The Teams BFF relays upstream's 409 as { detail, upstream_status }.
  put.mockRejectedValue(new ApiError(409, { detail: "Document was updated by someone else", upstream_status: 409 }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), " more");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/changed since you opened it/i);
  expect(screen.getByRole("textbox", { name: "Body" })).toHaveValue("Old body more");
  expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
});

it("any other failure is a plain error, not a conflict", async () => {
  put.mockRejectedValue(new ApiError(500, { detail: "upstream error" }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't save/i);
  expect(screen.queryByRole("button", { name: /reload/i })).toBeNull();
});

it("one save in flight", async () => {
  let release!: () => void;
  put.mockReturnValue(new Promise((r) => { release = () => r({ ...doc, latestRevisionId: "r4" }); }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  const save = await screen.findByRole("button", { name: "Save" });
  await userEvent.click(save);
  await userEvent.click(save);
  expect(put).toHaveBeenCalledTimes(1);
  expect(save).toBeDisabled();
  release();
});
