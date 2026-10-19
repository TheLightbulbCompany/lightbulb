import { tag } from "@expo/ui/swift-ui/modifiers";
import { fireEvent, render, screen } from "@testing-library/react-native";

import { materialFor } from "../SymbolView.android";
import { Picker, Section, Text } from "../swiftui.android";

// Jest resolves the iOS files by default, so these import the Android
// stand-ins by name.

it("maps SF Symbol names onto Material ones, dropping fill-style suffixes", () => {
  expect(materialFor("xmark")).toBe("close");
  expect(materialFor("mic.slash.fill")).toBe("mic_off");
  expect(materialFor("checkmark.circle")).toBe("check_circle");
  expect(materialFor("not.a.symbol")).toBeUndefined();
});

it("draws a menu picker as rows and reports the picked option", async () => {
  const onChange = jest.fn();
  await render(
    <Section title="Members">
      <Picker label="Friday" selection="active" onSelectionChange={onChange}>
        <Text modifiers={[tag("active")]}>Listening</Text>
        <Text modifiers={[tag("mentions")]}>Mentions only</Text>
      </Picker>
    </Section>,
  );
  expect(screen.getByText("MEMBERS")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Friday"));
  await fireEvent.press(screen.getByText("Mentions only"));
  expect(onChange).toHaveBeenCalledWith("mentions");
});
