import {
  BOARD_STATUSES,
  STATUS_LABELS,
  statusColors,
  type BoardStatus,
} from "../../lib/teamsData";
import { PickerSheet } from "./PickerSheet";

// ---------------------------------------------------------------------------
// Status picker bottom-sheet (contract §6 — the touch-friendly stand-in for
// drag). Thin wrapper over the generic PickerSheet, mapping the 7 board
// statuses to colored-dot options. Public API unchanged from T2a.
// ---------------------------------------------------------------------------

export interface StatusSheetProps {
  visible: boolean;
  /** Title of the issue being moved (context header), or null. */
  issueTitle: string | null;
  /** The issue's current status (checked + non-selectable no-op). */
  currentStatus: string | null | undefined;
  onSelect: (status: BoardStatus) => void;
  onClose: () => void;
}

const STATUS_OPTIONS = BOARD_STATUSES.map((status) => ({
  key: status,
  label: STATUS_LABELS[status],
  dotColor: statusColors(status).color,
  strikethrough: status === "cancelled",
}));

export function StatusSheet({
  visible,
  issueTitle,
  currentStatus,
  onSelect,
  onClose,
}: StatusSheetProps) {
  return (
    <PickerSheet
      visible={visible}
      heading="Move to status"
      subheading={issueTitle}
      options={STATUS_OPTIONS}
      selectedKey={currentStatus ?? null}
      onSelect={(key) => onSelect(key as BoardStatus)}
      onClose={onClose}
    />
  );
}
