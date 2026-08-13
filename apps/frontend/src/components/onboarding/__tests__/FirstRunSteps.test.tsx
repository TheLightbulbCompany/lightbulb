import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import { FirstRunSteps, type Mode, type Path } from "@/components/onboarding/FirstRunSteps";

// The classic two-path variant is covered end-to-end by the FirstRunFunnel
// suite; this file covers only the landing-hero `defaultPath` variant.
function Harness({ onSubmit }: { onSubmit: (mode: Mode) => void }) {
  const [path, setPath] = useState<Path | null>(null);
  const [input, setInput] = useState("");
  const [detail, setDetail] = useState("");
  return (
    <FirstRunSteps
      path={path}
      input={input}
      detail={detail}
      busy={false}
      mode="idea"
      error={null}
      onSetPath={setPath}
      onInput={setInput}
      onDetail={setDetail}
      onSubmit={onSubmit}
      defaultPath="new"
    />
  );
}

describe("FirstRunSteps defaultPath variant", () => {
  it("opens on the idea input with the quiet grow toggle — no picker, no back", () => {
    render(<Harness onSubmit={vi.fn()} />);

    expect(screen.getByPlaceholderText(/social media agency/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /start something new/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /back/i })).toBeNull();
    expect(
      screen.getByRole("button", { name: /already have a business/i }),
    ).toBeInTheDocument();
  });

  it("toggles to the grow path and back", () => {
    render(<Harness onSubmit={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /already have a business/i }));
    expect(screen.getByPlaceholderText(/yourcompany\.com/i)).toBeInTheDocument();
    // Grow has no "you pick" escape hatch.
    expect(screen.queryByRole("button", { name: /don.t know yet/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /starting from scratch/i }));
    expect(screen.getByPlaceholderText(/social media agency/i)).toBeInTheDocument();
  });

  it("submits the idea", () => {
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    fireEvent.change(screen.getByPlaceholderText(/social media agency/i), {
      target: { value: "A bakery for dogs" },
    });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    expect(onSubmit).toHaveBeenCalledWith("idea");
  });
});
