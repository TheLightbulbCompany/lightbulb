import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { ReactNode } from "react";

import { CreateIssueSheet } from "../CreateIssueSheet";

jest.mock("../../ui/BottomSheet", () => {
  const { View } = require("react-native");
  return { BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => visible ? <View>{children}</View> : null };
});

it("sends the picked work mode with the new issue", async () => {
  const onSubmit = jest.fn().mockResolvedValue(true);
  await render(<CreateIssueSheet visible mode="issue" onSubmit={onSubmit} onClose={jest.fn()} />);
  await fireEvent.changeText(screen.getByPlaceholderText("What needs doing?"), "Plan the launch");
  await fireEvent.press(screen.getByLabelText("Plan"));
  await fireEvent.press(screen.getByLabelText("Create issue"));
  await waitFor(() =>
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Plan the launch", workMode: "planning" }),
    ),
  );
});

it("defaults a sub-issue to the standard work mode", async () => {
  const onSubmit = jest.fn().mockResolvedValue(true);
  await render(<CreateIssueSheet visible mode="subissue" onSubmit={onSubmit} onClose={jest.fn()} />);
  await fireEvent.changeText(screen.getByPlaceholderText("What needs doing?"), "Draft the copy");
  await fireEvent.press(screen.getByLabelText("Create sub-issue"));
  await waitFor(() =>
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ workMode: "standard" })),
  );
});
