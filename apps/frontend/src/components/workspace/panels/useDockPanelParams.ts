"use client";

import { useEffect, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";

// dockview's onDidParametersChange fires with the RAW PARTIAL object passed
// to updateParameters (dockview-core panelApi: `updateParameters(parameters)`
// → `_onDidParametersChange.fire(parameters)`), NOT the merged parameter bag
// — so a host that replaces its state with the payload wipes every param the
// update didn't name (a `{ newIssue: undefined }` one-shot clear would blank
// teamsKey and unmount the whole panel body). Mirror dockview's own internal
// merge (dockviewPanel.update): spread over the previous params, then delete
// keys explicitly set to `undefined`.
export function useDockPanelParams<T extends object>(props: IDockviewPanelProps<T>): T {
  const [params, setParams] = useState<T>(props.params);
  // An update made before this panel mounted (the shell applies the URL command right after it
  // restores the layout) fires its event with nobody listening: dockview renders the panel once
  // with the restored params and then hands the merged bag in as props. Without this, a reload of
  // ?panel=chat&session= kept the restored params and opened the default room.
  const [handed, setHanded] = useState(props.params);
  if (handed !== props.params) { setHanded(props.params); setParams(props.params); }
  useEffect(() => {
    const d = props.api.onDidParametersChange((next) => {
      setParams((prev) => {
        const partial = next as Partial<T> & Record<string, unknown>;
        const merged: T = { ...prev, ...partial };
        for (const k of Object.keys(partial)) {
          if (partial[k] === undefined) {
            delete (merged as Record<string, unknown>)[k];
          }
        }
        return merged;
      });
    });
    return () => d.dispose();
  }, [props.api]);
  return params;
}
