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
