import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MessageList } from "../MessageList";

describe("MessageList load-earlier control", () => {
  it("renders the control only when older pages remain", () => {
    const { rerender } = render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder={false} />,
    );
    expect(screen.queryByRole("button", { name: /earlier messages/i })).toBeNull();

    rerender(<MessageList renderGroups={[]} agentName="Friday" hasOlder />);
    expect(screen.getByRole("button", { name: /earlier messages/i })).toBeTruthy();
  });

  it("invokes onLoadOlder when clicked", () => {
    const onLoadOlder = vi.fn();
    render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder onLoadOlder={onLoadOlder} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /earlier messages/i }));
    expect(onLoadOlder).toHaveBeenCalledOnce();
  });

  it("shows a pending label while a page is in flight", () => {
    render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder isLoadingOlder />,
    );
    expect(screen.getByText(/loading earlier messages/i)).toBeTruthy();
  });
});
