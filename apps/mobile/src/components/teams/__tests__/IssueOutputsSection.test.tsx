import { fireEvent, render, screen } from "@testing-library/react-native";

import { DocumentsSection } from "../IssueOutputsSection";
import type { CompanyMember, IssueDocument } from "../../../lib/teamsData";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "id" }));

const members: CompanyMember[] = [{ userId: "user_ada", paperclipUserId: "pc_ada", name: "Ada" }];
const doc = (over: Partial<IssueDocument>): IssueDocument => ({
  id: "d1", key: "plan", title: "Plan", body: "Hello", updatedAt: new Date().toISOString(), latestRevisionNumber: 2, ...over,
});

async function show(d: IssueDocument) {
  await render(<DocumentsSection documents={[d]} agents={[]} members={members} currentUserId="user_zed" onSave={jest.fn()} />);
}
const open = () => fireEvent.press(screen.getByRole("button", { name: "Plan" }));

it("names a person who saved the document", async () => {
  await show(doc({ updatedByUserId: "pc_ada" }));
  expect(screen.getByText(/^Ada · updated/)).toBeTruthy();
});

it("draws a card in an opened body with every control disabled", async () => {
  const body = 'Pick:\n```openui-lang\nroot = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])\n```';
  await show(doc({ body }));
  await open();
  expect(screen.getByRole("button", { name: "Studio" }).props.accessibilityState.disabled).toBe(true);
  expect(screen.getByRole("button", { name: "Starter" }).props.accessibilityState.disabled).toBe(true);
  expect(screen.queryByText(/root = Stack/)).toBeNull();
});

it("leaves a body without fences as it was", async () => {
  await show(doc({ body: "Just words" }));
  await open();
  expect(screen.getByText("Just words")).toBeTruthy();
});
