import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AgentPaperclipFields } from "../AgentPaperclipFields";
import type { PaperclipMeta } from "@/lib/paperclipMeta";

describe("AgentPaperclipFields", () => {
  const defaultMeta: PaperclipMeta = { role: "general" };

  it("renders all 12 Paperclip roles in the select", () => {
    render(<AgentPaperclipFields value={defaultMeta} onChange={() => {}} />);
    const select = screen.getByLabelText(/Role/i) as HTMLSelectElement;
    expect(select.options).toHaveLength(12);
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual([
      "ceo",
      "cto",
      "cmo",
      "cfo",
      "security",
      "engineer",
      "designer",
      "pm",
      "qa",
      "devops",
      "researcher",
      "general",
    ]);
  });

  it("emits onChange with the new role when select changes", () => {
    const onChange = vi.fn();
    render(<AgentPaperclipFields value={defaultMeta} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/Role/i), {
      target: { value: "engineer" },
    });
    expect(onChange).toHaveBeenCalledWith({ role: "engineer" });
  });

  it("emits onChange with new title on input change, preserving role", () => {
    const onChange = vi.fn();
    render(
      <AgentPaperclipFields
        value={{ role: "cmo", title: "Old" }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Title/i), {
      target: { value: "New" },
    });
    expect(onChange).toHaveBeenCalledWith({ role: "cmo", title: "New" });
  });

  it("emits onChange with new capabilities on textarea change", () => {
    const onChange = vi.fn();
    render(
      <AgentPaperclipFields
        value={{ role: "general" }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Capabilities/i), {
      target: { value: "Books Resy reservations." },
    });
    expect(onChange).toHaveBeenCalledWith({
      role: "general",
      capabilities: "Books Resy reservations.",
    });
  });

  it("disables all inputs when disabled prop is true", () => {
    render(
      <AgentPaperclipFields
        value={defaultMeta}
        onChange={() => {}}
        disabled
      />,
    );
    expect(screen.getByLabelText(/Role/i)).toBeDisabled();
    expect(screen.getByLabelText(/Title/i)).toBeDisabled();
    expect(screen.getByLabelText(/Capabilities/i)).toBeDisabled();
  });
});
