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
