"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { LAUNCH_REF_KEY } from "@/lib/launch";

/** `?ref=` wins and is persisted; otherwise whatever a previous visit stored. */
export function useLaunchRef(): string | null {
  const params = useSearchParams();
  const fromQuery = params.get("ref")?.trim().toUpperCase() || null;
  const [ref, setRef] = React.useState<string | null>(fromQuery);
  React.useEffect(() => {
    try {
      if (fromQuery) {
        window.localStorage.setItem(LAUNCH_REF_KEY, fromQuery);
        setRef(fromQuery);
      } else {
        setRef(window.localStorage.getItem(LAUNCH_REF_KEY));
      }
    } catch {
      /* storage unavailable */
    }
  }, [fromQuery]);
  return ref;
}
