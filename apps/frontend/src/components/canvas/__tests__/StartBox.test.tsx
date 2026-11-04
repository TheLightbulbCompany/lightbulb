import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StartBox } from "../StartBox";

describe("StartBox", () => {
  it("sends the request and opens the new app; the box clears", async () => {
    const onStart = vi.fn().mockResolvedValue({ app_id: "app_9" });
    const onOpen = vi.fn();
    render(<StartBox onStart={onStart} onOpen={onOpen} first={false} />);
    const box = screen.getByLabelText("What should we build?") as HTMLTextAreaElement;
    expect((screen.getByRole("button", { name: "Build it" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(box, { target: { value: "  a booking page  " } });
    fireEvent.click(screen.getByRole("button", { name: "Build it" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("app_9"));
    expect(onStart).toHaveBeenCalledWith("a booking page");
    expect(box.value).toBe("");
  });

  it("Enter sends, Shift+Enter does not", async () => {
    const onStart = vi.fn().mockResolvedValue({ app_id: "a" });
    render(<StartBox onStart={onStart} onOpen={vi.fn()} first />);
    const box = screen.getByLabelText("What should we build?");
    fireEvent.change(box, { target: { value: "a form" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(onStart).toHaveBeenCalledWith("a form"));
  });

  it("an idea fills the box", () => {
    render(<StartBox onStart={vi.fn()} onOpen={vi.fn()} first />);
    fireEvent.click(screen.getByRole("button", { name: "A booking page for 30-minute calls" }));
    expect((screen.getByLabelText("What should we build?") as HTMLTextAreaElement).value).toBe("A booking page for 30-minute calls");
  });

  it("a failure says so, keeps the text, raw reason under Details", async () => {
    const onStart = vi.fn().mockRejectedValue(new Error("402 plan"));
    render(<StartBox onStart={onStart} onOpen={vi.fn()} first={false} />);
    const box = screen.getByLabelText("What should we build?") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "a form" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect((await screen.findByRole("alert")).textContent).toContain("That didn't start.");
    expect(box.value).toBe("a form");
  });
});
