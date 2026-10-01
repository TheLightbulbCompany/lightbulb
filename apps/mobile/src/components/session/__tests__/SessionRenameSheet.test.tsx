import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LocalSession } from "@lightbulb/shared";
import type { ReactNode } from "react";

import { SessionRenameSheet } from "../SessionRenameSheet";

jest.mock("../../ui/BottomSheet", () => {
  const { View } = require("react-native");
  return { BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => visible ? <View>{children}</View> : null };
});

it("keeps the edited name after a failed save and retries it", async () => {
  const session = { sessionId: "s1", title: "Old name", displayTitle: null } as LocalSession;
  const onSave = jest.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(undefined);
  const onClose = jest.fn();
  await render(<SessionRenameSheet session={session} visible onClose={onClose} onSave={onSave} />);
  await fireEvent.changeText(screen.getByLabelText("Session name"), "New name");
  await fireEvent.press(screen.getByLabelText("Save session name"));
  await waitFor(() => expect(screen.getByText("Could not rename this session. Try again.")).toBeTruthy());
  expect(screen.getByLabelText("Session name").props.value).toBe("New name");
  await fireEvent.press(screen.getByLabelText("Save session name"));
  await waitFor(() => expect(onSave).toHaveBeenLastCalledWith("New name"));
  expect(onClose).toHaveBeenCalledTimes(1);
});
