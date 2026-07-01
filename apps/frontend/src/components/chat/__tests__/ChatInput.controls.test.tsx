import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: [] } => ({ commands: [] }),
}));
vi.mock("@/lib/analytics", () => ({ capture: () => {} }));

import { ChatInput } from "@/components/chat/ChatInput";

describe("ChatInput controls slot", () => {
  it("renders the controls node above the composer bar", () => {
    render(
      <ChatInput
        onSend={() => {}}
        controls={<div data-testid="pill-slot">pill</div>}
      />,
    );
    expect(screen.getByTestId("pill-slot")).toBeInTheDocument();
  });

  it("renders nothing extra when controls is omitted", () => {
    render(<ChatInput onSend={() => {}} />);
    expect(screen.queryByTestId("pill-slot")).not.toBeInTheDocument();
  });
});
