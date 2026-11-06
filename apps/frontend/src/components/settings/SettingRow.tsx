import type { ReactNode } from "react";

/** The settings row (settings-place.css): the label and a one-line consequence on the left, the control on the right,
 *  a hairline between rows, no card per setting. `htmlFor` ties the label to its control. */
export function SettingRow({ label, line, htmlFor, children }: { label: ReactNode; line?: ReactNode; htmlFor?: string; children?: ReactNode }) {
  return (
    <div className="settings-rows__row">
      <div className="settings-rows__text">
        {htmlFor ? <label htmlFor={htmlFor} className="settings-rows__label">{label}</label> : <p className="settings-rows__label">{label}</p>}
        {line && <p className="settings-rows__line">{line}</p>}
      </div>
      {children}
    </div>
  );
}
