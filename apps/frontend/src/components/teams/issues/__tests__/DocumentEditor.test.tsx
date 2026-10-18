import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
let latest: unknown;
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ put, mutate, read: (p: string | null) => ({ data: p ? latest : undefined, mutate: vi.fn() }) }) }));
import { DocumentEditor } from "../DocumentEditor";

const doc = { id: "d1", key: "plan", title: "Launch plan", body: "Old body", latestRevisionId: "r3", latestRevisionNumber: 3 };

beforeEach(() => { put.mockReset(); mutate.mockReset(); latest = undefined; });

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

const theirs = { ...doc, body: "Their body", latestRevisionId: "r4", latestRevisionNumber: 4, updatedByAgentId: "a1" };

it("a stale save keeps the draft, names who changed it and offers their version", async () => {
  // The Teams BFF relays upstream's 409 as { detail, upstream_status }.
  put.mockRejectedValue(new ApiError(409, { detail: "Document was updated by someone else", upstream_status: 409 }));
  latest = theirs;
  render(<DocumentEditor issueId="i1" document={doc} agents={[{ id: "a1", name: "Friday" } as never]} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), " more");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/changed since you opened it/i);
  expect(alert).toHaveTextContent(/Friday saved rev 4/);
  expect(screen.getByText("Their version (rev 4)")).toBeInTheDocument();
  expect(screen.getByText("Their body")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Body" })).toHaveValue("Old body more");
  expect(screen.getByRole("button", { name: "Save over their version" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Discard my draft" })).toBeInTheDocument();
});

it("Save over their version saves the same draft against their revision", async () => {
  put.mockRejectedValueOnce(new ApiError(409, { detail: "stale", upstream_status: 409 }));
  put.mockResolvedValueOnce({ ...doc, latestRevisionId: "r5" });
  latest = theirs;
  const onSaved = vi.fn();
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={onSaved} onCancel={vi.fn()} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), " more");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  await userEvent.click(await screen.findByRole("button", { name: "Save over their version" }));
  expect(put).toHaveBeenCalledTimes(2);
  expect(put.mock.calls[1][1]).toMatchObject({ body: "Old body more", baseRevisionId: "r4" });
  expect(screen.queryByText(/someone else/)).toBeNull();
  await waitFor(() => expect(onSaved).toHaveBeenCalled());
});

it("any other failure is a plain error, not a conflict", async () => {
  put.mockRejectedValue(new ApiError(500, { detail: "upstream error" }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), "!");
  await userEvent.click(await screen.findByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't save/i);
  expect(screen.queryByRole("button", { name: /reload/i })).toBeNull();
});

it("one save in flight", async () => {
  let release!: () => void;
  put.mockReturnValue(new Promise((r) => { release = () => r({ ...doc, latestRevisionId: "r4" }); }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), "!");
  const save = await screen.findByRole("button", { name: "Save" });
  await userEvent.click(save);
  await userEvent.click(save);
  expect(put).toHaveBeenCalledTimes(1);
  expect(save).toBeDisabled();
  release();
});

async function conflictOn(err: ApiError, props: Record<string, unknown> = {}) {
  put.mockRejectedValue(err);
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} {...props} />);
  await userEvent.type(await screen.findByRole("textbox", { name: "Body" }), " more");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  return screen.findByRole("alert");
}

it("a locked document says who locked it, keeps the draft and allows a retry", async () => {
  latest = { ...doc, lockedAt: "2026-10-16T10:00:00Z", lockedByAgentId: "a1" };
  const alert = await conflictOn(new ApiError(409, { detail: "Document is locked", upstream_status: 409 }), { agents: [{ id: "a1", name: "Friday" }] });
  expect(alert).toHaveTextContent("Locked by Friday. Your draft is kept here; save again once it is unlocked.");
  expect(alert).not.toHaveTextContent(/changed since/);
  expect(screen.queryByRole("button", { name: "Save over their version" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "Body" })).toHaveValue("Old body more");
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

it("while the re-read runs the notice still offers Discard", async () => {
  latest = undefined;
  const alert = await conflictOn(new ApiError(409, { detail: "stale", upstream_status: 409 }));
  expect(alert).toHaveTextContent(/Loading their version/);
  expect(screen.getByRole("button", { name: "Discard my draft" })).toBeInTheDocument();
});

it("names a human by Paperclip id, and says You for the current user", async () => {
  latest = { ...theirs, updatedByAgentId: null, updatedByUserId: "pc-1" };
  const members = [{ userId: "user_1", paperclipUserId: "pc-1", name: "Ada Lovelace" }];
  expect(await conflictOn(new ApiError(409, { detail: "stale", upstream_status: 409 }), { members })).toHaveTextContent("Ada Lovelace saved rev 4");
  cleanup();
  expect(await conflictOn(new ApiError(409, { detail: "stale", upstream_status: 409 }), { members, currentUserId: "user_1" })).toHaveTextContent("You saved rev 4");
});

it("a 422 names the body limit (the title field already caps itself) and a wording match on another status is not a conflict", async () => {
  const alert = await conflictOn(new ApiError(422, { detail: "body too long, locked" }));
  expect(alert).toHaveTextContent(/512 KB/);
  expect(alert).not.toHaveTextContent(/Locked by/);
});

it("Save is disabled until something changes", async () => {
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "Save" })).toBeDisabled();
});
