jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

const mockPut = jest.fn().mockResolvedValue({});
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ put: mockPut }),
}));

import { updateIssueDocument } from "../teamsData";

it("PUTs the document with the revision the person opened", async () => {
  const body = {
    title: "Plan",
    format: "markdown" as const,
    body: "# Plan",
    changeSummary: "Edited by Ada",
    baseRevisionId: "rev_7",
  };
  await updateIssueDocument({} as never, "iss 1", "plan", body);
  expect(mockPut).toHaveBeenCalledWith("/teams/issues/iss%201/documents/plan", body);
});

import { documentChangeSummary } from "../teamsData";

it("names the editor, falling back to first name then You", () => {
  expect(documentChangeSummary({ fullName: "Ada Lovelace", firstName: "Ada" })).toBe("Edited by Ada Lovelace");
  expect(documentChangeSummary({ fullName: "", firstName: "Ada" })).toBe("Edited by Ada");
  expect(documentChangeSummary(null)).toBe("Edited by You");
});
