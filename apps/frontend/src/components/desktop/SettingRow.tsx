"use client";

import { useId, type ReactNode } from "react";

import { Switch } from "@/components/ui/switch";

/** One row of a Settings card: title, optional description, an optional switch on the
 *  right (a switch takes effect at once), and optional content below. Rows in a card
 *  get a hairline between them (settings.css `.settings-row`). */
export function SettingRow({ id, title, description, toggle, children }: {
  id?: string;
  title: ReactNode;
  description?: ReactNode;
  toggle?: { checked: boolean; disabled?: boolean; onChange: (next: boolean) => void };
  children?: ReactNode;
}) {
  const titleId = useId();
  const descId = useId();
  return (
    <div className="settings-row" id={id}>
      <div className="settings-row-main">
        <div className="settings-row-text">
          <div className="settings-toggle-label" id={titleId}>{title}</div>
          {description ? <div className="settings-toggle-desc" id={descId}>{description}</div> : null}
        </div>
        {toggle ? (
          <Switch
            checked={toggle.checked}
            disabled={toggle.disabled}
            onCheckedChange={toggle.onChange}
            aria-labelledby={titleId}
            aria-describedby={description ? descId : undefined}
          />
        ) : null}
      </div>
      {children}
    </div>
  );
}
