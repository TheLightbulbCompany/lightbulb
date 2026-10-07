// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { MissingPanel } from "../panels/MissingPanel";

type MissingPanelProps = React.ComponentProps<typeof MissingPanel>;

describe("MissingPanel", () => {
  it("renders the unavailable message and closes on click", () => {
    const close = vi.fn();
    const props = { params: {}, api: { close } } as unknown as MissingPanelProps;
    render(<MissingPanel {...props} />);

    expect(screen.getByText(/no longer available/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(close).toHaveBeenCalledTimes(1);
  });
});
