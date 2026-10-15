import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { IDockviewPanelHeaderProps } from "dockview-react";
import { DockTab } from "../DockTab";
import { MESSAGES_PANEL_ID } from "../panelRegistry";

function renderTab(id: string) {
  const api = { id, title: id, onDidTitleChange: () => ({ dispose() {} }), close: vi.fn() };
  const { container } = render(
    <div role="tab"><DockTab {...({ api } as unknown as IDockviewPanelHeaderProps)} /></div>,
  );
  return { tab: container.querySelector('[role="tab"]') as HTMLElement, close: container.querySelector(".dv-default-tab-action") as HTMLElement, api };
}

afterEach(() => window.history.replaceState(null, "", "/"));

it("picking another tab drops the ?panel= that named the panel left behind", async () => {
  window.history.replaceState(null, "", "/chat?panel=teams-artifacts&utm_source=x");
  const { tab } = renderTab(MESSAGES_PANEL_ID);
  await userEvent.click(tab);
  expect(window.location.search).toBe("?utm_source=x");
});

it("leaves the URL alone for the tab it names, and for the close button", async () => {
  window.history.replaceState(null, "", "/chat?panel=teams-artifacts");
  const { tab } = renderTab("teams-artifacts");
  await userEvent.click(tab);
  expect(window.location.search).toBe("?panel=teams-artifacts");

  const other = renderTab("teams-issues");
  await userEvent.click(other.close);
  expect(other.api.close).toHaveBeenCalled();
  expect(window.location.search).toBe("?panel=teams-artifacts");
});
