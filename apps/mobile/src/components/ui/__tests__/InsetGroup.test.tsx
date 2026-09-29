import { render, screen } from "@testing-library/react-native";

import { chatPalette } from "../../../config/theme";
import { InsetSectionHeader, insetRowStyle } from "../InsetGroup";

const p = chatPalette.dark;

describe("insetRowStyle", () => {
  it("rounds the top of the first row and the bottom of the last", () => {
    expect(insetRowStyle(p, 0, 3)).toMatchObject({ borderTopWidth: 1, borderTopLeftRadius: 12, backgroundColor: p.surface, borderColor: p.edge });
    expect(insetRowStyle(p, 2, 3)).toMatchObject({ borderBottomWidth: 1, borderBottomRightRadius: 12, marginBottom: 8 });
    expect(insetRowStyle(p, 1, 3)).not.toHaveProperty("borderTopWidth");
    expect(insetRowStyle(p, 0, 1)).toMatchObject({ borderTopWidth: 1, borderBottomWidth: 1 });
  });
});

it("titles a section as a header with its count", async () => {
  await render(<InsetSectionHeader title="Needs you" count={2} palette={p} />);
  expect(screen.getByRole("header", { name: /Needs you/ })).toBeTruthy();
  expect(screen.getByText("2")).toBeTruthy();
});
