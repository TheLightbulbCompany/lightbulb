"use client";

import { MailPanel } from "@/components/mail/MailPanel";
import type { PanelProps } from "../panelComponents";

// Mounted only while the pane shows it, so it is always the one on screen.
export function MailDockPanel({ params }: PanelProps) {
  return <MailPanel item={params.item ?? null} visible />;
}
