import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError, type LocalSession } from "@lightbulb/shared";
import { SessionShareLink } from "../SessionShareLink";

const session = { sessionId: "s1", provider: "claude" } as LocalSession;
const url = "https://dev.thelightbulb.company/s#" + "t".repeat(43);
/** The control once the link's audience is read: it names none before that. */
const who = async () => { await waitFor(() => expect(screen.getByLabelText("Who can see this")).toBeEnabled()); return screen.getByLabelText("Who can see this"); };

const make = (link: "workspace" | "anyone" | null) => ({
  getLocalSessionLink: vi.fn(async () => { if (!link) throw new ApiError(404, { code: "LOCAL_SESSION_NOT_FOUND" }); return { url, audience: link }; }),
  putLocalSessionLink: vi.fn(async (_: string, audience?: "workspace" | "anyone") => ({ url, audience: audience ?? link ?? "workspace" })),
  setLocalSessionSharing: vi.fn(async (_: string, shared: boolean) => ({ ...session, shared }) as LocalSession),
  setLocalSessionPickup: vi.fn(async (_: string, allowed: boolean) => ({ ...session, pickup: { allowed } }) as LocalSession),
});

it("is one control: Only me, My workspace, Anyone with the link, and one link to copy", async () => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  const client = make(null);
  render(<SessionShareLink session={session} client={client} />);
  expect(await who()).toHaveValue("workspace"); // a shared session with no link yet is the workspace's
  expect([...(await who()).querySelectorAll("option")].map(o => o.textContent)).toEqual(["Only me", "My workspace", "Anyone with the link"]);
  expect(screen.queryByRole("alert")).toBeNull(); // a 404 is "no link yet", not an error
  expect(screen.queryByRole("button", { name: /Turn off/ })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Copy link" }));
  expect(client.putLocalSessionLink).toHaveBeenCalledWith("s1", undefined); // never narrows a link
  expect(await screen.findByText("Link copied. Only people in your workspace can open it.")).toBeInTheDocument();
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(url);
  await userEvent.selectOptions(await who(), "anyone");
  expect(client.putLocalSessionLink).toHaveBeenLastCalledWith("s1", "anyone");
  expect(await screen.findByRole("note")).toHaveTextContent("Anyone with the link can watch this terminal live without signing in. They cannot type.");
  await userEvent.click(screen.getByRole("button", { name: "Copy link" }));
  expect(await screen.findByText("Link copied. Anyone with the link can watch.")).toBeInTheDocument();
  await userEvent.selectOptions(await who(), "workspace");
  expect(client.putLocalSessionLink).toHaveBeenLastCalledWith("s1", "workspace");
});

it("Only me takes a second click, cuts the link back, and hides the link until it is shared again", async () => {
  const client = make("anyone");
  render(<SessionShareLink session={session} client={client} />);
  expect(await who()).toHaveValue("anyone");
  await userEvent.selectOptions(await who(), "me");
  expect(client.setLocalSessionSharing).not.toHaveBeenCalled(); // one click never cuts everyone off
  await userEvent.click(screen.getByRole("button", { name: "Click again to stop sharing" }));
  expect(client.setLocalSessionSharing).toHaveBeenCalledWith("s1", false);
  expect(await screen.findByText("Only you can see this session now.")).toBeInTheDocument();
  expect(await who()).toHaveValue("me");
  expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
  // Back to the workspace: sharing resumes, and the link is not public again.
  await userEvent.selectOptions(await who(), "workspace");
  expect(client.setLocalSessionSharing).toHaveBeenLastCalledWith("s1", true);
  expect(await who()).toHaveValue("workspace");
  expect(client.putLocalSessionLink).not.toHaveBeenCalled();
});

it("picking another audience instead of the second click leaves the session shared", async () => {
  const client = make("workspace");
  render(<SessionShareLink session={session} client={client} />);
  await userEvent.selectOptions(await who(), "me");
  await userEvent.selectOptions(await who(), "workspace");
  expect(screen.queryByRole("button", { name: "Click again to stop sharing" })).toBeNull();
  expect(client.setLocalSessionSharing).not.toHaveBeenCalled();
});

it("a paused session reads Only me", async () => {
  render(<SessionShareLink session={{ ...session, shared: false }} client={make("workspace")} />);
  expect(await who()).toHaveValue("me");
});

it("Let people continue this session is off by default and is the owner's own switch", async () => {
  const client = make("anyone");
  render(<SessionShareLink session={session} client={client} />);
  const toggle = await screen.findByRole("switch", { name: "Let people continue this session" });
  expect(toggle).not.toBeChecked();
  await userEvent.click(toggle);
  expect(client.setLocalSessionPickup).toHaveBeenCalledWith("s1", true);
  expect(await screen.findByRole("switch", { name: "Let people continue this session" })).toBeChecked();
});

it("has no continue switch on a Codex session, and warns about a private repository only when a copy can be taken", async () => {
  const codex = render(<SessionShareLink session={{ ...session, provider: "codex" }} client={make("anyone")} />);
  await who();
  expect(screen.queryByRole("switch")).toBeNull();
  codex.unmount();
  render(<SessionShareLink session={{ ...session, pickup: { allowed: true, privateRepo: true } }} client={make("anyone")} />);
  expect(await screen.findByText("This conversation includes code Claude read from your private repository.")).toBeInTheDocument();
});

it("draws nothing without the link routes", () => {
  const { container } = render(<SessionShareLink session={session} client={{}} />);
  expect(container).toBeEmptyDOMElement();
});

// Codex on #3946: a failed read left the control on "My workspace" over a link that is open to anyone.
it("names no audience until the link is read, and a failed read offers a retry", async () => {
  let fail = true;
  const client = { ...make("anyone"), getLocalSessionLink: vi.fn(async () => { if (fail) throw new ApiError(500, null, "boom"); return { url, audience: "anyone" as const }; }) };
  render(<SessionShareLink session={session} client={client} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load who can see this session.");
  const control = screen.getByLabelText("Who can see this");
  expect(control).toBeDisabled();
  expect(control).not.toHaveValue("workspace");
  expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await who()).toHaveValue("anyone");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(client.putLocalSessionLink).not.toHaveBeenCalled();
});

// Codex on #3946: a paused session's control is usable while its link is still being read.
it("a read that lands after the owner's choice does not put the old audience back", async () => {
  let land!: (link: { url: string; audience: "workspace" }) => void;
  const client = { ...make("workspace"), getLocalSessionLink: vi.fn(() => new Promise<{ url: string; audience: "workspace" }>(resolve => { land = resolve; })) };
  render(<SessionShareLink session={{ ...session, shared: false }} client={client} />);
  await userEvent.selectOptions(await who(), "anyone");
  expect(client.putLocalSessionLink).toHaveBeenLastCalledWith("s1", "anyone");
  expect(await who()).toHaveValue("anyone");
  await act(async () => { land({ url, audience: "workspace" }); });
  expect(await who()).toHaveValue("anyone");
  expect(screen.getByRole("note")).toHaveTextContent("Anyone with the link can watch this terminal live");
});

// Codex on #3946: the failed change had cancelled the read in flight, and nothing read again.
it("a change that fails while the link is still being read reads it again, never Loading for good", async () => {
  const reads = [new Promise<never>(() => {}), Promise.resolve({ url, audience: "workspace" as const })];
  const client = {
    ...make("workspace"),
    getLocalSessionLink: vi.fn(() => reads.shift()!),
    putLocalSessionLink: vi.fn(async () => { throw new ApiError(500, null, "boom"); }),
  };
  render(<SessionShareLink session={{ ...session, shared: false }} client={client} />);
  await userEvent.selectOptions(await who(), "anyone"); // resumes sharing, then the link change fails
  expect(await who()).toHaveValue("workspace"); // what the server holds
  expect(client.getLocalSessionLink).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status")).toHaveTextContent("Could not change who can see this. Try again.");
});

it("a paused session reads Only me even when the link cannot be read", async () => {
  const client = { ...make("anyone"), getLocalSessionLink: vi.fn(async () => { throw new ApiError(500, null, "boom"); }) };
  render(<SessionShareLink session={{ ...session, shared: false }} client={client} />);
  expect(await who()).toHaveValue("me");
});
