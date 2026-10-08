import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError, type LocalSession } from "@lightbulb/shared";
import { SessionShareLink } from "../SessionShareLink";

const session = { sessionId: "s1", provider: "claude" } as LocalSession;
const url = "https://dev.thelightbulb.company/s#" + "t".repeat(43);

it("Share makes the one link and copies it; the audience switch warns; Turn off removes it", async () => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  const client = {
    getLocalSessionLink: vi.fn(async () => { throw new ApiError(404, { code: "LOCAL_SESSION_NOT_FOUND" }); }),
    putLocalSessionLink: vi.fn(async (_: string, audience?: "workspace" | "anyone") => ({ url, audience: audience ?? "workspace" })),
    deleteLocalSessionLink: vi.fn(async () => {}),
  };
  render(<SessionShareLink session={session} client={client} />);
  await userEvent.click(await screen.findByRole("button", { name: "Share" }));
  expect(client.putLocalSessionLink).toHaveBeenCalledWith("s1", undefined); // never narrows a link
  expect(screen.queryByRole("alert")).toBeNull(); // a 404 is "no link yet", not an error
  // ISOA-732: the owner is told who can open what they just copied, and sees it next to the link.
  expect(await screen.findByText("Link copied. Only people in your workspace can open it.")).toBeInTheDocument();
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(url);
  expect(screen.getByText("Only people signed in to your workspace can open it. Anyone else is asked to sign in and then cannot open it.")).toBeInTheDocument();
  expect(screen.getByLabelText("Who can open it")).toHaveValue("workspace");
  await userEvent.selectOptions(screen.getByLabelText("Who can open it"), "anyone");
  expect(client.putLocalSessionLink).toHaveBeenLastCalledWith("s1", "anyone");
  expect(await screen.findByRole("note")).toHaveTextContent("Anyone with the link sees this session's title, who runs it, the tool and its state, and can continue it with a copy of the conversation");
  expect(screen.getByRole("status")).toHaveTextContent("Anyone with the link can open it.");
  await userEvent.click(screen.getByRole("button", { name: "Copy link" }));
  expect(await screen.findByText("Link copied. Anyone with the link can open it.")).toBeInTheDocument();
  // One click never turns a link off: it stops working for everyone who has it.
  await userEvent.click(screen.getByRole("button", { name: "Turn off link" }));
  expect(client.deleteLocalSessionLink).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Click again to turn off" }));
  expect(client.deleteLocalSessionLink).toHaveBeenCalledWith("s1");
  expect(await screen.findByText("Link turned off. It no longer opens for anyone.")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Share" })).toBeInTheDocument();
});

it("promises no copy of the conversation on a Codex link", async () => {
  const client = { getLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn() };
  render(<SessionShareLink session={{ ...session, provider: "codex" }} client={client} />);
  const note = await screen.findByRole("note");
  expect(note).toHaveTextContent("Anyone with the link sees this session's title, who runs it, the tool and its state.");
  expect(note).not.toHaveTextContent("copy of the conversation");
});

it("warns that a private repository's code is in the conversation", async () => {
  const client = { getLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn() };
  render(<SessionShareLink session={{ ...session, pickup: { allowed: true, privateRepo: true } }} client={client} />);
  expect(await screen.findByText("This conversation includes code Claude read from your private repository.")).toBeInTheDocument();
});

it("shows an existing link's audience without creating one", async () => {
  const client = { getLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn() };
  render(<SessionShareLink session={session} client={client} />);
  expect(await screen.findByLabelText("Who can open it")).toHaveValue("anyone");
  expect(client.putLocalSessionLink).not.toHaveBeenCalled();
});

it("draws nothing without the link routes", () => {
  const { container } = render(<SessionShareLink session={session} client={{}} />);
  expect(container).toBeEmptyDOMElement();
});

it("a failed read says so, and Share keeps the link's audience", async () => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  const client = {
    getLocalSessionLink: vi.fn(async () => { throw new ApiError(500, null, "boom"); }),
    putLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })),
    deleteLocalSessionLink: vi.fn(),
  };
  render(<SessionShareLink session={session} client={client} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this session's link.");
  await userEvent.click(screen.getByRole("button", { name: "Share" }));
  expect(client.putLocalSessionLink).toHaveBeenCalledWith("s1", undefined);
  expect(await screen.findByLabelText("Who can open it")).toHaveValue("anyone");
  expect(await screen.findByText("Link copied. Anyone with the link can open it.")).toBeInTheDocument();
});
