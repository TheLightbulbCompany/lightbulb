import { describe, expect, it } from "vitest";

import { describePanel } from "../panelRegistry";
import { placeTabTitle } from "../tabTitle";
import { placeOf, type Slot } from "../workspaceController";

// The tab title per place, built from the same descriptors the shell opens.
function titleFor(panelKey: string | null, chatName: string | null = null): string {
  const d = panelKey ? describePanel(panelKey, {}) : null;
  const main: Slot | null = d ? { panelId: d.panelId, component: d.component, title: d.title, params: d.params } : null;
  return placeTabTitle({ place: placeOf(main), main }, chatName);
}

describe("placeTabTitle", () => {
  it("names each place", () => {
    expect({
      home: titleFor("home"),
      activity: titleFor("activity"),
      team: titleFor("teams-agents"),
      library: titleFor("teams-artifacts"),
      canvas: titleFor("canvas"),
      work: titleFor("teams-routines"),
      project: titleFor(null, "Launch plan"),
      dm: titleFor(null, "Growth"),
      nothingOpen: titleFor(null),
    }).toEqual({
      home: "Home · Lightbulb",
      activity: "Activity · Lightbulb",
      team: "Team · Lightbulb",
      library: "Library · Lightbulb",
      canvas: "Canvas · Lightbulb",
      work: "Work · Lightbulb",
      project: "Launch plan · Lightbulb",
      dm: "Growth · Lightbulb",
      nothingOpen: "Lightbulb",
    });
  });

});
