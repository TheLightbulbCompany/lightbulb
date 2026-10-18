import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { ReactNode } from "react";
import { Alert } from "react-native";

import { ApiError } from "@lightbulb/shared";
import { DocumentEditSheet } from "../DocumentEditSheet";

jest.mock("../../ui/BottomSheet", () => {
  const { View } = require("react-native");
  return { BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => visible ? <View>{children}</View> : null };
});

const doc = { id: "d1", key: "plan", title: "Plan", body: "old" };

it("saves the edited title and body", async () => {
  const onSave = jest.fn().mockResolvedValue(undefined);
  const onClose = jest.fn();
  await render(<DocumentEditSheet document={doc} onSave={onSave} onClose={onClose} />);
  await fireEvent.changeText(screen.getByLabelText("Document body"), "new text");
  await fireEvent.press(screen.getByLabelText("Save"));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith("Plan", "new text"));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

it("on a 409 says it changed and keeps the draft open", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  const onSave = jest.fn().mockRejectedValue(new ApiError(409, {}, "conflict"));
  const onClose = jest.fn();
  await render(<DocumentEditSheet document={doc} onSave={onSave} onClose={onClose} />);
  await fireEvent.changeText(screen.getByLabelText("Document body"), "mine");
  await fireEvent.press(screen.getByLabelText("Save"));
  await waitFor(() =>
    expect(alert).toHaveBeenCalledWith(expect.any(String), expect.stringContaining("changed since you opened it")),
  );
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Document body").props.value).toBe("mine");
});

it("shows the usual failure alert for other errors", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  const onSave = jest.fn().mockRejectedValue(new ApiError(500, {}, "boom"));
  await render(<DocumentEditSheet document={doc} onSave={onSave} onClose={jest.fn()} />);
  await fireEvent.press(screen.getByLabelText("Save"));
  await waitFor(() => expect(alert).toHaveBeenCalled());
  expect(alert.mock.calls.at(-1)?.[1]).not.toContain("changed since you opened it");
});
