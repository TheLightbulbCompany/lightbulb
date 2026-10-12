import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LocalSession, SessionControlClient } from "@lightbulb/shared";

import { SessionApprovalCard } from "../SessionApprovalCard";

const mockDigest = jest.fn(async (_algorithm: string, _text: string) => "abc");
jest.mock("expo-crypto", () => ({
  CryptoDigestAlgorithm: { SHA256: "SHA-256" },
  digestStringAsync: (algorithm: string, text: string) => mockDigest(algorithm, text),
  randomUUID: () => "r1",
}));

const base = { approvalId: "a1", tool: "Edit", summary: "src/x.ts", inputDigest: "abc", exact: false, modes: [] };
const session = (pendingApproval: object) => ({
  sessionId: "s1", provider: "claude", status: "waiting-approval", generation: "g", pendingApproval,
}) as unknown as LocalSession;
const client = () => ({ submitLocalSessionCommand: jest.fn(async () => ({ state: "dispatched" })) }) as unknown as SessionControlClient & { submitLocalSessionCommand: jest.Mock };

it("shows an edit's diff and allows it when the digest of what is shown matches", async () => {
  const c = client();
  const input = { file_path: "/Users/me/src/x.ts", old_string: "a", new_string: "b" };
  await render(<SessionApprovalCard session={session({ ...base, full: true, input })} client={c} allowed own ownerName="Leo" />);
  expect(screen.getByText("-a")).toBeTruthy();
  expect(screen.getByText("+b")).toBeTruthy();
  expect(mockDigest).toHaveBeenCalledWith("SHA-256", '{"file_path":"/Users/me/src/x.ts","new_string":"b","old_string":"a"}');
  await fireEvent.press(await screen.findByLabelText("Approve"));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ kind: "approve", decision: "allow", shownDigest: "abc" }));
});

it("a call that is not full stays deny-only with the partial copy", async () => {
  await render(<SessionApprovalCard session={session({ ...base, tool: "Bash", full: false, input: { command: "git push …" } })} client={client()} allowed own={false} ownerName="Leo" />);
  expect(screen.getByText("git push …")).toBeTruthy();
  expect(screen.getByText("Lightbulb shows only part of this call. Approve it on Leo’s Mac; you can deny it here.")).toBeTruthy();
  expect(screen.queryByLabelText("Approve")).toBeNull();
});

it("stays deny-only when what is shown does not hash to the prompt's digest", async () => {
  mockDigest.mockResolvedValueOnce("other");
  await render(<SessionApprovalCard session={session({ ...base, full: true, input: { content: "x" }, tool: "Write" })} client={client()} allowed own ownerName="Leo" />);
  expect(await screen.findByText("Lightbulb shows only part of this call. Approve it on your Mac; you can deny it here.")).toBeTruthy();
  expect(screen.queryByLabelText("Approve")).toBeNull();
});

it("shows a Bash call's flags, and that it runs outside the sandbox, before allowing it", async () => {
  const input = { command: "make deploy", run_in_background: true, dangerouslyDisableSandbox: true };
  await render(<SessionApprovalCard session={session({ ...base, tool: "Bash", full: true, input })} client={client()} allowed own ownerName="Leo" />);
  expect(screen.getByText("Runs outside the sandbox")).toBeTruthy();
  expect(screen.getByText("Runs in the background")).toBeTruthy();
  expect(await screen.findByLabelText("Approve")).toBeTruthy();
});

it("shows the whole JSON for a key no friendly view draws, and allows it only on a digest match", async () => {
  const input = { command: "ls", sneaky: "yes" };
  mockDigest.mockClear();
  mockDigest.mockResolvedValueOnce("other");
  const first = await render(<SessionApprovalCard session={session({ ...base, tool: "Bash", full: true, input })} client={client()} allowed own ownerName="Leo" />);
  expect(screen.getByText(JSON.stringify(input, null, 2))).toBeTruthy();
  await waitFor(() => expect(mockDigest).toHaveBeenCalledTimes(1));
  expect(screen.queryByLabelText("Approve")).toBeNull();
  await first.unmount();
  await render(<SessionApprovalCard session={session({ ...base, tool: "Bash", full: true, input })} client={client()} allowed own ownerName="Leo" />);
  expect(screen.getByText(JSON.stringify(input, null, 2))).toBeTruthy();
  expect(await screen.findByLabelText("Approve")).toBeTruthy();
});
