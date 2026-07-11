import React from "react";
import { Text } from "react-native";
import { render, screen } from "@testing-library/react-native";

// Proves @testing-library/react-native can mount a component under jest-expo.
//
// NOTE for component-test authors: RNTL 14's `render()` is ASYNC (it drives
// concurrent React via react-test-renderer). You MUST await it before querying:
// `await render(<Foo />); screen.getByText(...)`.
// Calling it synchronously returns a pending Promise and every query throws.
describe("RNTL harness", () => {
  it("mounts a trivial component and queries it", async () => {
    await render(<Text>hello-rntl</Text>);
    expect(screen.getByText("hello-rntl")).toBeTruthy();
  });
});
