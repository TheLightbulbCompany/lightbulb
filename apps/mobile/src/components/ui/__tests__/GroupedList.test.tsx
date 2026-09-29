import { render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { GroupedList, useGroupedPalette } from "../GroupedList";

function Probe() {
  return <Text>{useGroupedPalette().fg}</Text>;
}

it("hands the dark palette to rows inside a dark list", async () => {
  await render(
    <GroupedList colorScheme="dark">
      <Probe />
    </GroupedList>,
  );
  expect(screen.getByText(chatPalette.dark.fg)).toBeTruthy();
});

it("stays light by default and outside any list", async () => {
  await render(
    <>
      <GroupedList>
        <Probe />
      </GroupedList>
      <Probe />
    </>,
  );
  expect(screen.getAllByText(chatPalette.light.fg)).toHaveLength(2);
});
