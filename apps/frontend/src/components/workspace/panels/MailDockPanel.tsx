"use client";

import { useCallback, useSyncExternalStore } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { MailPanel } from "@/components/mail/MailPanel";
import { useDockPanelParams } from "./useDockPanelParams";

export function MailDockPanel(props: IDockviewPanelProps<{ item?: string }>) {
  const params = useDockPanelParams(props);
  const { api } = props;
  // A tab behind another is mounted but not visible: "new" only moves while it is shown.
  const subscribe = useCallback((listener: () => void) => {
    const subscription = api.onDidVisibilityChange(listener);
    return () => subscription.dispose();
  }, [api]);
  const visible = useSyncExternalStore(subscribe, () => api.isVisible, () => false);
  return <MailPanel item={params.item ?? null} visible={visible} />;
}
