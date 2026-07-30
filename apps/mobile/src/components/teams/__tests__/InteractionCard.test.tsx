import { fireEvent, render, screen } from "@testing-library/react-native";

// InteractionCard imports teamsData, which imports expo-router
// (useFocusEffect) at module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import type { IssueInteraction } from "../../../lib/teamsData";
import { InteractionCard } from "../InteractionCard";

const agents = [{ id: "agent_1", name: "Friday" }];

function makeInteraction(overrides: Partial<IssueInteraction>): IssueInteraction {
  return {
    id: "int_1",
    issueId: "iss_1",
    kind: "request_confirmation",
    status: "pending",
    createdByAgentId: "agent_1",
    createdAt: new Date().toISOString(),
    payload: { prompt: "Ship it?" },
    ...overrides,
  } as IssueInteraction;
}

describe("InteractionCard", () => {
  it("pending confirmation: Accept fires onAccept with no selection", async () => {
    const onAccept = jest.fn();
    await render(
      <InteractionCard
        interaction={makeInteraction({})}
        agents={agents}
        onAccept={onAccept}
        onReject={jest.fn()}
        onRespond={jest.fn()}
      />,
    );
    expect(screen.getByText("Ship it?")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Accept"));
    expect(onAccept).toHaveBeenCalledWith("int_1", undefined);
  });

  it("pending confirmation: Decline without required reason rejects directly", async () => {
    const onReject = jest.fn();
    await render(
      <InteractionCard
        interaction={makeInteraction({})}
        agents={agents}
        onAccept={jest.fn()}
        onReject={onReject}
        onRespond={jest.fn()}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Decline"));
    expect(onReject).toHaveBeenCalledWith("int_1");
  });

  it("checkbox confirmation: accept carries toggled selectedOptionIds", async () => {
    const onAccept = jest.fn();
    await render(
      <InteractionCard
        interaction={makeInteraction({
          kind: "request_checkbox_confirmation",
          payload: {
            prompt: "Which ones?",
            options: [
              { id: "a", label: "Option A" },
              { id: "b", label: "Option B" },
            ],
            defaultSelectedOptionIds: ["a", "b"],
          },
        })}
        agents={agents}
        onAccept={onAccept}
        onReject={jest.fn()}
        onRespond={jest.fn()}
      />,
    );
    // Deselect B, keep A.
    await fireEvent.press(screen.getByLabelText("Option B"));
    await fireEvent.press(screen.getByLabelText("Accept"));
    expect(onAccept).toHaveBeenCalledWith("int_1", { selectedOptionIds: ["a"] });
  });

  it("ask_user_questions: submit blocked until required answers, then responds", async () => {
    const onRespond = jest.fn();
    await render(
      <InteractionCard
        interaction={makeInteraction({
          kind: "ask_user_questions",
          payload: {
            questions: [
              {
                id: "q1",
                prompt: "Pick one",
                selectionMode: "single",
                options: [
                  { id: "o1", label: "First" },
                  { id: "o2", label: "Second" },
                ],
              },
            ],
          },
        })}
        agents={agents}
        onAccept={jest.fn()}
        onReject={jest.fn()}
        onRespond={onRespond}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Submit answers"));
    expect(onRespond).not.toHaveBeenCalled();

    await fireEvent.press(screen.getByLabelText("Second"));
    await fireEvent.press(screen.getByLabelText("Submit answers"));
    expect(onRespond).toHaveBeenCalledWith("int_1", [
      { questionId: "q1", optionIds: ["o2"] },
    ]);
  });

  it("suggest_tasks: accept carries the selected clientKeys", async () => {
    const onAccept = jest.fn();
    await render(
      <InteractionCard
        interaction={makeInteraction({
          kind: "suggest_tasks",
          payload: {
            tasks: [
              { clientKey: "t1", title: "Task one" },
              { clientKey: "t2", title: "Task two" },
            ],
          },
        })}
        agents={agents}
        onAccept={onAccept}
        onReject={jest.fn()}
        onRespond={jest.fn()}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Task two"));
    await fireEvent.press(screen.getByLabelText("Accept"));
    expect(onAccept).toHaveBeenCalledWith("int_1", { selectedClientKeys: ["t1"] });
  });

  it("resolved interaction collapses to a summary with no action buttons", async () => {
    await render(
      <InteractionCard
        interaction={makeInteraction({
          status: "accepted",
          result: { outcome: "accepted" },
        })}
        agents={agents}
        onAccept={jest.fn()}
        onReject={jest.fn()}
        onRespond={jest.fn()}
      />,
    );
    expect(screen.getByText("Confirmed request")).toBeTruthy();
    expect(screen.queryByLabelText("Accept")).toBeNull();
    expect(screen.queryByLabelText("Decline")).toBeNull();
  });
});
