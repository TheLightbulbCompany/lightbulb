"use client";

import * as React from "react";
import { useApi } from "@/lib/api";
import type { BlockedPayload } from "@/hooks/useProvisioningState";
import { fetchLaunchInfo } from "@/lib/launch";
import type { LaunchMe } from "@/lib/launch";
import { LaunchStatusLines } from "@/components/launch/LaunchStatusLines";
import { useReferralAttribution } from "@/components/launch/useReferralAttribution";
import "@/components/launch/launch.css";

/**
 * The in-app pre-drop held state — rendered by ProvisioningStepper when the
 * provision gate returns `blocked.code === "launch_pending"` (see
 * `core/services/launch_hold.py`). `/launch/me` is authed (useApi); `/launch`
 * is public and read with plain fetch, mirroring how `/launch`'s own
 * `MyLaunchCard` sources the same two reads.
 */
export function LaunchHeldCard({ blocked }: { blocked: BlockedPayload }) {
  const api = useApi();
  const [me, setMe] = React.useState<LaunchMe | null>(null);
  const [dropAt, setDropAt] = React.useState<string | null>(null);
  // Either read failing leaves the status lines unrenderable forever; fall back
  // to the gate's own copy rather than a permanent "Loading…".
  const [failed, setFailed] = React.useState(false);
  useReferralAttribution(api, me);

  React.useEffect(() => {
    let alive = true;
    api
      .get("/launch/me")
      .then((m) => {
        if (alive) setMe(m as LaunchMe);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [api]);

  React.useEffect(() => {
    let alive = true;
    fetchLaunchInfo()
      .then((info) => {
        if (!alive) return;
        if (info?.drop_at) setDropAt(info.drop_at);
        else setFailed(true);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="lc-card" data-testid="launch-held-card">
      {me && dropAt ? (
        <LaunchStatusLines me={me} dropAt={dropAt} />
      ) : failed ? (
        <p className="ld-lede">{blocked.title}</p>
      ) : (
        <p className="lc-fine">Loading your status…</p>
      )}
      <p className="lc-fine" style={{ marginTop: 16 }}>{blocked.message}</p>
    </div>
  );
}
