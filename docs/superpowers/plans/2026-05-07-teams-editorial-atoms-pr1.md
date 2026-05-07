# Teams Editorial Atom Library (PR 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the editorial-tokenized atom layer that the per-panel ports (PRs 2-10) will compose with. Closes the consistency leg of the user's complaints (buttons feel wrong, tabs differ across panels) without touching panel logic yet.

**Architecture:** 11 atoms in `apps/frontend/src/components/teams/shell/`, split into editorial primitives (button/input/select/dialog/card) and panel scaffolding (masthead/toolbar/tabs/section/empty-state/data-table). Editorial primitives wrap shadcn components with our cream/forest/ink tokens; panel scaffolding composes the primitives. Vitest snapshot + behavior tests per atom. No visual lab — first port (PR 2 Inbox) is the design-review surface in dev.

**Tech Stack:** Next.js 16 App Router, React 19, Tailwind CSS v4, shadcn/ui (Radix-based), lucide-react, vitest + @testing-library/react. Editorial tokens already defined in `apps/frontend/src/app/globals.css`.

**Spec:** `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md`

**Branch:** `spec/teams-port-editorial-atoms` (already cut off main; this plan extends it)

---

## File Structure

**Created (frontend):**
- `apps/frontend/src/components/teams/shell/EditorialButton.tsx` — primary/secondary/ghost/destructive × sm/md/lg/icon
- `apps/frontend/src/components/teams/shell/EditorialInput.tsx` — input with label, helptext, error states
- `apps/frontend/src/components/teams/shell/EditorialSelect.tsx` — shadcn Select restyled
- `apps/frontend/src/components/teams/shell/EditorialDialog.tsx` — shadcn Dialog with editorial-card body
- `apps/frontend/src/components/teams/shell/EditorialCard.tsx` — wraps editorial-card class with optional hover
- `apps/frontend/src/components/teams/shell/PanelMasthead.tsx` — overline + Lora-italic title + date + actions
- `apps/frontend/src/components/teams/shell/PanelToolbar.tsx` — search + filter chips + view toggles + CTA
- `apps/frontend/src/components/teams/shell/PanelTabs.tsx` — tabs row with count badges
- `apps/frontend/src/components/teams/shell/PanelSection.tsx` — collapsible group with chevron + count
- `apps/frontend/src/components/teams/shell/PanelEmptyState.tsx` — icon + Lora headline + supporting copy + CTA
- `apps/frontend/src/components/teams/shell/PanelDataTable.tsx` — hover row, sortable header, selection
- `apps/frontend/src/components/teams/shell/index.ts` — barrel re-export

**Created (shadcn primitives — only if missing):**
- `apps/frontend/src/components/ui/select.tsx` — shadcn Select primitive
- `apps/frontend/src/components/ui/tabs.tsx` — shadcn Tabs primitive
- `apps/frontend/src/components/ui/table.tsx` — shadcn Table primitive
- `apps/frontend/src/components/ui/collapsible.tsx` — shadcn Collapsible primitive

**Created (tests):**
- `apps/frontend/tests/unit/components/teams/shell/EditorialButton.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/EditorialInput.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/EditorialSelect.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/EditorialDialog.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/EditorialCard.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelMasthead.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelToolbar.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelTabs.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelSection.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelEmptyState.test.tsx`
- `apps/frontend/tests/unit/components/teams/shell/PanelDataTable.test.tsx`

**Modified:**
- `apps/frontend/src/app/globals.css` — token audit: keep `--cream`/`--forest`/`--ink`/`--mute`/`--edge`/`--warn`/`--danger`; add `--ink-soft` (already partially used) + `--forest-tint` for hover states; document each token's purpose

---

## Task 0: Verify environment + install missing shadcn primitives

**Files:**
- Modify: `apps/frontend/src/components/ui/` (add missing primitives only)

- [ ] **Step 1: Confirm worktree branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git status
git log --oneline -1
```

Expected: on branch `spec/teams-port-editorial-atoms`, top commit is the spec edits commit (`0e0b015f` or later).

- [ ] **Step 2: Check which shadcn primitives are already present**

```sh
ls apps/frontend/src/components/ui/
```

Expected output includes: `alert-dialog.tsx`, `badge.tsx`, `button.tsx`, `checkbox.tsx`, `dialog.tsx`, `dropdown-menu.tsx`, `input.tsx`, `popover.tsx`, `scroll-area.tsx`, `textarea.tsx`. If `select.tsx`, `tabs.tsx`, `table.tsx`, `collapsible.tsx` are missing, add them in step 3.

- [ ] **Step 3: Add missing shadcn primitives**

For each missing primitive (run from `apps/frontend/`):

```sh
cd apps/frontend
pnpm dlx shadcn@latest add select tabs table collapsible
```

Accept defaults. The shadcn CLI installs into `src/components/ui/`.

- [ ] **Step 4: Verify @testing-library/react is installed**

```sh
grep -E '@testing-library/(react|jest-dom|user-event)' apps/frontend/package.json
```

Expected: `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event` all present. If any are missing, install:

```sh
cd apps/frontend && pnpm add -D @testing-library/react @testing-library/user-event
```

- [ ] **Step 5: Run existing tests to verify clean baseline**

```sh
cd apps/frontend && pnpm test
```

Expected: all tests pass (the panel tests in `apps/frontend/tests/unit/teams/`).

- [ ] **Step 6: Commit (only if shadcn primitives or test deps were added)**

```sh
git add apps/frontend/src/components/ui/ apps/frontend/package.json apps/frontend/pnpm-lock.yaml
git commit -m "chore(frontend): add shadcn primitives + testing-library deps for teams shell atoms"
```

Skip the commit if nothing changed in step 3/4.

---

## Task 1: EditorialButton

The first atom: every other atom that has a clickable element will use this. Variants follow the existing `editorial-btn` / `editorial-btn--primary` / `editorial-btn--secondary` classes already in `globals.css`.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/EditorialButton.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/EditorialButton.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/EditorialButton.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialButton } from "@/components/teams/shell/EditorialButton";

describe("EditorialButton", () => {
  it("renders children", () => {
    render(<EditorialButton>Click me</EditorialButton>);
    expect(screen.getByRole("button", { name: "Click me" })).toBeInTheDocument();
  });

  it("applies primary variant by default", () => {
    render(<EditorialButton>X</EditorialButton>);
    expect(screen.getByRole("button")).toHaveClass("editorial-btn", "editorial-btn--primary");
  });

  it.each(["primary", "secondary", "ghost", "destructive"] as const)(
    "applies %s variant class",
    (variant) => {
      render(<EditorialButton variant={variant}>X</EditorialButton>);
      expect(screen.getByRole("button")).toHaveClass(`editorial-btn--${variant}`);
    },
  );

  it.each(["sm", "md", "lg", "icon"] as const)("applies %s size class", (size) => {
    render(<EditorialButton size={size}>X</EditorialButton>);
    expect(screen.getByRole("button")).toHaveClass(`editorial-btn--size-${size}`);
  });

  it("forwards onClick", async () => {
    const onClick = vi.fn();
    render(<EditorialButton onClick={onClick}>X</EditorialButton>);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("respects disabled", async () => {
    const onClick = vi.fn();
    render(<EditorialButton onClick={onClick} disabled>X</EditorialButton>);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("forwards ref", () => {
    const ref = { current: null as HTMLButtonElement | null };
    render(<EditorialButton ref={ref}>X</EditorialButton>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test EditorialButton -- --run
```

Expected: FAIL with "Cannot find module @/components/teams/shell/EditorialButton".

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/EditorialButton.tsx
"use client";

import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export type EditorialButtonVariant = "primary" | "secondary" | "ghost" | "destructive";
export type EditorialButtonSize = "sm" | "md" | "lg" | "icon";

interface EditorialButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: EditorialButtonVariant;
  size?: EditorialButtonSize;
}

export const EditorialButton = forwardRef<HTMLButtonElement, EditorialButtonProps>(
  ({ variant = "primary", size = "md", className, children, ...rest }, ref) => {
    return (
      <button
        ref={ref}
        className={cn(
          "editorial-btn",
          `editorial-btn--${variant}`,
          `editorial-btn--size-${size}`,
          className,
        )}
        {...rest}
      >
        {children}
      </button>
    );
  },
);

EditorialButton.displayName = "EditorialButton";
```

- [ ] **Step 4: Add the size + ghost + destructive classes to globals.css**

Existing `globals.css` only has `editorial-btn`, `editorial-btn--primary`, `editorial-btn--secondary`. Append:

```css
/* in apps/frontend/src/app/globals.css, after the existing editorial-btn rules */
.editorial-btn--ghost {
  background: transparent;
  color: var(--ink);
  border: 1px solid transparent;
}
.editorial-btn--ghost:hover:not(:disabled) {
  background: var(--edge);
}

.editorial-btn--destructive {
  background: var(--danger);
  color: var(--cream);
  border: 1px solid var(--danger);
}
.editorial-btn--destructive:hover:not(:disabled) {
  filter: brightness(0.95);
}

.editorial-btn--size-sm  { padding: 4px 10px; font-size: 12px; line-height: 1.2; }
.editorial-btn--size-md  { padding: 6px 14px; font-size: 13px; line-height: 1.3; }
.editorial-btn--size-lg  { padding: 8px 18px; font-size: 14px; line-height: 1.3; }
.editorial-btn--size-icon { padding: 6px;     width: 32px; height: 32px; }

.editorial-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

- [ ] **Step 5: Run test, verify pass**

```sh
cd apps/frontend && pnpm test EditorialButton -- --run
```

Expected: PASS, 9 assertions.

- [ ] **Step 6: Commit**

```sh
git add apps/frontend/src/components/teams/shell/EditorialButton.tsx \
         apps/frontend/tests/unit/components/teams/shell/EditorialButton.test.tsx \
         apps/frontend/src/app/globals.css
git commit -m "feat(teams-shell): EditorialButton with 4 variants × 4 sizes"
```

---

## Task 2: EditorialInput

**Files:**
- Create: `apps/frontend/src/components/teams/shell/EditorialInput.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/EditorialInput.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/EditorialInput.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialInput } from "@/components/teams/shell/EditorialInput";

describe("EditorialInput", () => {
  it("renders label associated with input", () => {
    render(<EditorialInput label="Name" id="name" />);
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });

  it("renders helptext", () => {
    render(<EditorialInput label="Email" helpText="we never share" />);
    expect(screen.getByText("we never share")).toBeInTheDocument();
  });

  it("renders error state", () => {
    render(<EditorialInput label="X" error="required" />);
    expect(screen.getByText("required")).toHaveAttribute("role", "alert");
    expect(screen.getByLabelText("X")).toHaveAttribute("aria-invalid", "true");
  });

  it("forwards onChange", async () => {
    const onChange = vi.fn();
    render(<EditorialInput label="X" onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("X"), "hi");
    expect(onChange).toHaveBeenCalled();
  });

  it("forwards ref", () => {
    const ref = { current: null as HTMLInputElement | null };
    render(<EditorialInput label="X" ref={ref} />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test EditorialInput -- --run
```

Expected: FAIL with module-not-found.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/EditorialInput.tsx
"use client";

import { forwardRef, useId } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

interface EditorialInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "id"> {
  label: string;
  helpText?: ReactNode;
  error?: string;
  id?: string;
}

export const EditorialInput = forwardRef<HTMLInputElement, EditorialInputProps>(
  ({ label, helpText, error, id, className, ...rest }, ref) => {
    const generatedId = useId();
    const inputId = id ?? generatedId;
    const helpId = `${inputId}-help`;
    const errorId = `${inputId}-error`;

    return (
      <div className="flex flex-col gap-1.5">
        <label htmlFor={inputId} className="text-[11px] font-semibold uppercase tracking-[0.12em]" style={{ color: "var(--mute)" }}>
          {label}
        </label>
        <input
          id={inputId}
          ref={ref}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={cn(helpText && helpId, error && errorId) || undefined}
          className={cn(
            "editorial-input rounded-md border px-3 py-2 text-[13px] outline-none transition-colors",
            "focus:ring-2 focus:ring-[var(--forest)]/20 focus:border-[var(--forest)]",
            error && "border-[var(--danger)] focus:ring-[var(--danger)]/20 focus:border-[var(--danger)]",
            !error && "border-[var(--edge)]",
            "bg-[var(--cream)] text-[var(--ink)]",
            className,
          )}
          {...rest}
        />
        {helpText && !error && (
          <p id={helpId} className="text-[11px]" style={{ color: "var(--mute)" }}>
            {helpText}
          </p>
        )}
        {error && (
          <p id={errorId} role="alert" className="text-[11px]" style={{ color: "var(--danger)" }}>
            {error}
          </p>
        )}
      </div>
    );
  },
);

EditorialInput.displayName = "EditorialInput";
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test EditorialInput -- --run
```

Expected: PASS, 5 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/EditorialInput.tsx \
         apps/frontend/tests/unit/components/teams/shell/EditorialInput.test.tsx
git commit -m "feat(teams-shell): EditorialInput with label, helptext, error states"
```

---

## Task 3: EditorialSelect

**Files:**
- Create: `apps/frontend/src/components/teams/shell/EditorialSelect.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/EditorialSelect.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/EditorialSelect.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialSelect } from "@/components/teams/shell/EditorialSelect";

const OPTIONS = [
  { value: "low", label: "Low" },
  { value: "med", label: "Medium" },
  { value: "high", label: "High" },
];

describe("EditorialSelect", () => {
  it("renders the placeholder when no value", () => {
    render(<EditorialSelect options={OPTIONS} placeholder="Pick one" />);
    expect(screen.getByText("Pick one")).toBeInTheDocument();
  });

  it("renders the chosen value's label", () => {
    render(<EditorialSelect options={OPTIONS} value="med" onValueChange={() => {}} />);
    expect(screen.getByText("Medium")).toBeInTheDocument();
  });

  it("calls onValueChange when an option is selected", async () => {
    const onValueChange = vi.fn();
    render(
      <EditorialSelect options={OPTIONS} value="low" onValueChange={onValueChange} />
    );
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(await screen.findByText("High"));
    expect(onValueChange).toHaveBeenCalledWith("high");
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test EditorialSelect -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/EditorialSelect.tsx
"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

export interface EditorialSelectOption {
  value: string;
  label: string;
}

interface EditorialSelectProps {
  options: EditorialSelectOption[];
  value?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}

export function EditorialSelect({
  options,
  value,
  onValueChange,
  placeholder,
  className,
  disabled,
}: EditorialSelectProps) {
  return (
    <Select value={value} onValueChange={onValueChange} disabled={disabled}>
      <SelectTrigger
        className={cn(
          "border-[var(--edge)] bg-[var(--cream)] text-[var(--ink)]",
          "focus:ring-[var(--forest)]/20 focus:border-[var(--forest)]",
          className,
        )}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent className="border-[var(--edge)] bg-[var(--cream)]">
        {options.map((opt) => (
          <SelectItem
            key={opt.value}
            value={opt.value}
            className="text-[var(--ink)] focus:bg-[var(--edge)] focus:text-[var(--ink)]"
          >
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test EditorialSelect -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/EditorialSelect.tsx \
         apps/frontend/tests/unit/components/teams/shell/EditorialSelect.test.tsx
git commit -m "feat(teams-shell): EditorialSelect with token-themed shadcn Select"
```

---

## Task 4: EditorialCard

(Smaller, simpler — wraps the existing `editorial-card` class with optional hover variant.)

**Files:**
- Create: `apps/frontend/src/components/teams/shell/EditorialCard.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/EditorialCard.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/EditorialCard.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EditorialCard } from "@/components/teams/shell/EditorialCard";

describe("EditorialCard", () => {
  it("renders children inside the editorial-card class", () => {
    render(<EditorialCard><span>inside</span></EditorialCard>);
    const child = screen.getByText("inside");
    expect(child.parentElement).toHaveClass("editorial-card");
  });

  it("adds editorial-card-hover when hover prop set", () => {
    render(<EditorialCard hover><span>inside</span></EditorialCard>);
    expect(screen.getByText("inside").parentElement).toHaveClass("editorial-card-hover");
  });

  it("forwards className", () => {
    render(<EditorialCard className="custom"><span>inside</span></EditorialCard>);
    expect(screen.getByText("inside").parentElement).toHaveClass("custom");
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test EditorialCard -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/EditorialCard.tsx
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

interface EditorialCardProps extends HTMLAttributes<HTMLDivElement> {
  hover?: boolean;
}

export function EditorialCard({ hover, className, children, ...rest }: EditorialCardProps) {
  return (
    <div
      className={cn("editorial-card", hover && "editorial-card-hover", className)}
      {...rest}
    >
      {children}
    </div>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test EditorialCard -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/EditorialCard.tsx \
         apps/frontend/tests/unit/components/teams/shell/EditorialCard.test.tsx
git commit -m "feat(teams-shell): EditorialCard wrapping editorial-card class"
```

---

## Task 5: EditorialDialog

(Wraps shadcn Dialog with the editorial-card body styling.)

**Files:**
- Create: `apps/frontend/src/components/teams/shell/EditorialDialog.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/EditorialDialog.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/EditorialDialog.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";

describe("EditorialDialog", () => {
  it("does not render content when closed", () => {
    render(
      <EditorialDialog open={false} onOpenChange={() => {}} title="Hello">
        body content
      </EditorialDialog>
    );
    expect(screen.queryByText("body content")).not.toBeInTheDocument();
  });

  it("renders title + content when open", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="Hello">
        body content
      </EditorialDialog>
    );
    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.getByText("body content")).toBeInTheDocument();
  });

  it("calls onOpenChange when escape pressed", async () => {
    const onOpenChange = vi.fn();
    render(
      <EditorialDialog open onOpenChange={onOpenChange} title="X">
        body
      </EditorialDialog>
    );
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test EditorialDialog -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/EditorialDialog.tsx
"use client";

import type { ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface EditorialDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}

export function EditorialDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
}: EditorialDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-[var(--edge)] bg-[var(--cream)] text-[var(--ink)] sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="font-editorial-serif text-[22px] leading-tight" style={{ color: "var(--ink)" }}>
            {title}
          </DialogTitle>
          {description && (
            <DialogDescription className="text-[13px]" style={{ color: "var(--mute)" }}>
              {description}
            </DialogDescription>
          )}
        </DialogHeader>
        <div className="space-y-3 py-2">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-[var(--edge)] pt-3">
            {footer}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test EditorialDialog -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/EditorialDialog.tsx \
         apps/frontend/tests/unit/components/teams/shell/EditorialDialog.test.tsx
git commit -m "feat(teams-shell): EditorialDialog with editorial-card body + Lora title"
```

---

## Task 6: PanelMasthead

The panel header. Matches the existing pattern in `DashboardPanel.tsx:144-169`.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelMasthead.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelMasthead.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelMasthead.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { PanelMasthead } from "@/components/teams/shell/PanelMasthead";

describe("PanelMasthead", () => {
  it("renders overline + title", () => {
    render(<PanelMasthead overline="Overview" title="Dashboard" />);
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Dashboard", level: 1 })).toBeInTheDocument();
  });

  it("renders meta on the right", () => {
    render(<PanelMasthead overline="X" title="Y" meta="Mon, May 7" />);
    expect(screen.getByText("Mon, May 7")).toBeInTheDocument();
  });

  it("renders actions slot", () => {
    render(
      <PanelMasthead overline="X" title="Y" actions={<button>Create</button>} />
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelMasthead -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelMasthead.tsx
import type { ReactNode } from "react";

interface PanelMastheadProps {
  overline: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}

export function PanelMasthead({ overline, title, meta, actions }: PanelMastheadProps) {
  return (
    <header
      className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-end sm:justify-between"
      style={{ borderBottom: "1px solid var(--edge)" }}
    >
      <div className="space-y-1">
        <p
          className="text-[10px] font-semibold uppercase tracking-[0.12em]"
          style={{ color: "var(--mute)" }}
        >
          {overline}
        </p>
        <h1
          className="font-editorial-serif text-5xl leading-none"
          style={{ color: "var(--ink)" }}
        >
          {title}
        </h1>
      </div>
      <div className="flex items-center gap-3 sm:pb-1">
        {meta && (
          <p className="text-xs tabular-nums font-mono" style={{ color: "var(--mute)" }}>
            {meta}
          </p>
        )}
        {actions}
      </div>
    </header>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelMasthead -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelMasthead.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelMasthead.test.tsx
git commit -m "feat(teams-shell): PanelMasthead — overline + Lora title + meta + actions"
```

---

## Task 7: PanelTabs

Tabs row used by Inbox / Routines / Issues / Costs. Wraps shadcn Tabs primitive.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelTabs.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelTabs.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelTabs.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelTabs } from "@/components/teams/shell/PanelTabs";

const TABS = [
  { value: "default", label: "Default", count: 3 },
  { value: "unread", label: "Unread", count: 1 },
  { value: "archived", label: "Archived" },
];

describe("PanelTabs", () => {
  it("renders all tabs with counts when present", () => {
    render(<PanelTabs tabs={TABS} value="default" onValueChange={() => {}} />);
    expect(screen.getByRole("tab", { name: /default/i })).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("marks the active tab", () => {
    render(<PanelTabs tabs={TABS} value="unread" onValueChange={() => {}} />);
    expect(screen.getByRole("tab", { name: /unread/i })).toHaveAttribute("data-state", "active");
  });

  it("calls onValueChange when a tab is clicked", async () => {
    const onValueChange = vi.fn();
    render(<PanelTabs tabs={TABS} value="default" onValueChange={onValueChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /archived/i }));
    expect(onValueChange).toHaveBeenCalledWith("archived");
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelTabs -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelTabs.tsx
"use client";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export interface PanelTabDefinition {
  value: string;
  label: string;
  count?: number;
}

interface PanelTabsProps {
  tabs: PanelTabDefinition[];
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
}

export function PanelTabs({ tabs, value, onValueChange, className }: PanelTabsProps) {
  return (
    <Tabs value={value} onValueChange={onValueChange} className={className}>
      <TabsList
        className="h-auto justify-start gap-1 bg-transparent p-0"
        style={{ borderBottom: "1px solid var(--edge)" }}
      >
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.value}
            value={tab.value}
            className={cn(
              "rounded-none border-b-2 border-transparent bg-transparent px-3 py-2 text-[13px]",
              "data-[state=active]:border-[var(--forest)] data-[state=active]:bg-transparent data-[state=active]:text-[var(--ink)]",
              "data-[state=inactive]:text-[var(--mute)]",
            )}
          >
            <span>{tab.label}</span>
            {typeof tab.count === "number" && tab.count > 0 && (
              <span
                className="ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums"
                style={{
                  background: "var(--edge)",
                  color: "var(--ink)",
                }}
              >
                {tab.count}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelTabs -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelTabs.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelTabs.test.tsx
git commit -m "feat(teams-shell): PanelTabs with count badges + forest active underline"
```

---

## Task 8: PanelToolbar

Toolbar above tables / lists — search input on the left, filter chips and view toggles in the middle, primary CTA on the right.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelToolbar.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelToolbar.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelToolbar.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelToolbar } from "@/components/teams/shell/PanelToolbar";

describe("PanelToolbar", () => {
  it("renders the search placeholder", () => {
    render(
      <PanelToolbar searchValue="" onSearchChange={() => {}} searchPlaceholder="Find issue..." />
    );
    expect(screen.getByPlaceholderText("Find issue...")).toBeInTheDocument();
  });

  it("calls onSearchChange when typing", async () => {
    const onSearchChange = vi.fn();
    render(
      <PanelToolbar searchValue="" onSearchChange={onSearchChange} searchPlaceholder="search" />
    );
    await userEvent.type(screen.getByPlaceholderText("search"), "h");
    expect(onSearchChange).toHaveBeenCalledWith("h");
  });

  it("renders the trailing actions slot", () => {
    render(
      <PanelToolbar
        searchValue=""
        onSearchChange={() => {}}
        searchPlaceholder="x"
        actions={<button>+ Create</button>}
      />
    );
    expect(screen.getByRole("button", { name: "+ Create" })).toBeInTheDocument();
  });

  it("renders the filters slot", () => {
    render(
      <PanelToolbar
        searchValue=""
        onSearchChange={() => {}}
        searchPlaceholder="x"
        filters={<span data-testid="filters">F</span>}
      />
    );
    expect(screen.getByTestId("filters")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelToolbar -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelToolbar.tsx
"use client";

import { Search } from "lucide-react";
import type { ReactNode } from "react";

interface PanelToolbarProps {
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  filters?: ReactNode;
  actions?: ReactNode;
}

export function PanelToolbar({
  searchValue,
  onSearchChange,
  searchPlaceholder,
  filters,
  actions,
}: PanelToolbarProps) {
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex flex-1 items-center gap-2">
        <div className="relative flex-1 max-w-[320px]">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5"
            style={{ color: "var(--mute)" }}
          />
          <input
            type="text"
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={searchPlaceholder}
            className="w-full rounded-md border border-[var(--edge)] bg-[var(--cream)] py-1.5 pl-7 pr-2 text-[13px] text-[var(--ink)] outline-none transition-colors focus:border-[var(--forest)] focus:ring-2 focus:ring-[var(--forest)]/20"
          />
        </div>
        {filters && <div className="flex items-center gap-1.5">{filters}</div>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelToolbar -- --run
```

Expected: PASS, 4 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelToolbar.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelToolbar.test.tsx
git commit -m "feat(teams-shell): PanelToolbar — search + filters slot + actions slot"
```

---

## Task 9: PanelSection

Collapsible group with chevron + label + count + content. Used by Routines (group by agent / project) and Inbox (group by date).

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelSection.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelSection.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelSection.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelSection } from "@/components/teams/shell/PanelSection";

describe("PanelSection", () => {
  it("renders label + count + children when open", () => {
    render(
      <PanelSection label="Today" count={2} open onOpenChange={() => {}}>
        <div>row</div>
      </PanelSection>
    );
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("row")).toBeInTheDocument();
  });

  it("hides children when closed", () => {
    render(
      <PanelSection label="X" open={false} onOpenChange={() => {}}>
        <div>row</div>
      </PanelSection>
    );
    expect(screen.queryByText("row")).not.toBeVisible();
  });

  it("toggles via the chevron trigger", async () => {
    const onOpenChange = vi.fn();
    render(
      <PanelSection label="X" open onOpenChange={onOpenChange}>
        <div>row</div>
      </PanelSection>
    );
    await userEvent.click(screen.getByRole("button", { name: /X/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelSection -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelSection.tsx
"use client";

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

interface PanelSectionProps {
  label: ReactNode;
  count?: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}

export function PanelSection({ label, count, open, onOpenChange, children }: PanelSectionProps) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange}>
      <CollapsibleTrigger
        className="flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-[var(--edge)]"
        style={{ borderBottom: "1px solid var(--edge)" }}
      >
        <ChevronRight
          className="h-3.5 w-3.5 shrink-0 transition-transform [[data-state=open]>&]:rotate-90"
          style={{ color: "var(--mute)" }}
        />
        <span
          className="text-[12px] font-semibold uppercase tracking-[0.08em]"
          style={{ color: "var(--ink)" }}
        >
          {label}
        </span>
        {typeof count === "number" && (
          <span className="text-[11px] tabular-nums" style={{ color: "var(--mute)" }}>
            {count}
          </span>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelSection -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelSection.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelSection.test.tsx
git commit -m "feat(teams-shell): PanelSection — chevron + label + count Collapsible"
```

---

## Task 10: PanelEmptyState

The user's #1 complaint: every empty state needs structure. Icon + Lora-italic headline + supporting copy + CTA button.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelEmptyState.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelEmptyState.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelEmptyState.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Inbox } from "lucide-react";
import { PanelEmptyState } from "@/components/teams/shell/PanelEmptyState";

describe("PanelEmptyState", () => {
  it("renders headline + supporting copy", () => {
    render(
      <PanelEmptyState
        icon={Inbox}
        headline="Inbox zero."
        supporting="Nothing pending right now."
      />
    );
    expect(screen.getByText("Inbox zero.")).toBeInTheDocument();
    expect(screen.getByText("Nothing pending right now.")).toBeInTheDocument();
  });

  it("does not render a CTA when no action prop", () => {
    render(<PanelEmptyState icon={Inbox} headline="x" supporting="y" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders + fires the CTA when provided", async () => {
    const onAction = vi.fn();
    render(
      <PanelEmptyState
        icon={Inbox}
        headline="x"
        supporting="y"
        action={{ label: "Create routine", onClick: onAction }}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Create routine" }));
    expect(onAction).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelEmptyState -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelEmptyState.tsx
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { EditorialButton } from "./EditorialButton";

interface PanelEmptyStateAction {
  label: ReactNode;
  onClick: () => void;
}

interface PanelEmptyStateProps {
  icon: LucideIcon;
  headline: ReactNode;
  supporting: ReactNode;
  action?: PanelEmptyStateAction;
}

export function PanelEmptyState({ icon: Icon, headline, supporting, action }: PanelEmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <div
        className="flex h-12 w-12 items-center justify-center rounded-md"
        style={{ background: "var(--edge)" }}
      >
        <Icon className="h-6 w-6" style={{ color: "var(--mute)" }} />
      </div>
      <h2
        className="font-editorial-serif text-[22px] italic leading-tight"
        style={{ color: "var(--ink)" }}
      >
        {headline}
      </h2>
      <p className="max-w-[360px] text-[13px]" style={{ color: "var(--mute)" }}>
        {supporting}
      </p>
      {action && (
        <EditorialButton variant="primary" onClick={action.onClick} className="mt-2">
          {action.label}
        </EditorialButton>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelEmptyState -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelEmptyState.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelEmptyState.test.tsx
git commit -m "feat(teams-shell): PanelEmptyState — icon + Lora headline + supporting + CTA"
```

---

## Task 11: PanelDataTable

The data table primitive — used by Issues, Routines, Members, Skills. Hover row, sortable header, optional selection.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/PanelDataTable.tsx`
- Test: `apps/frontend/tests/unit/components/teams/shell/PanelDataTable.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/tests/unit/components/teams/shell/PanelDataTable.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelDataTable, type PanelDataColumn } from "@/components/teams/shell/PanelDataTable";

interface Row {
  id: string;
  name: string;
  count: number;
}

const ROWS: Row[] = [
  { id: "1", name: "Alpha", count: 3 },
  { id: "2", name: "Bravo", count: 1 },
];

const COLUMNS: PanelDataColumn<Row>[] = [
  { key: "name", header: "Name", cell: (row) => row.name, sortable: true },
  { key: "count", header: "Count", cell: (row) => row.count, sortable: true },
];

describe("PanelDataTable", () => {
  it("renders rows + columns", () => {
    render(<PanelDataTable<Row> rows={ROWS} columns={COLUMNS} rowKey={(r) => r.id} />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /name/i })).toBeInTheDocument();
  });

  it("calls onSortChange when a sortable header is clicked", async () => {
    const onSortChange = vi.fn();
    render(
      <PanelDataTable<Row>
        rows={ROWS}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        sortKey="name"
        sortDir="asc"
        onSortChange={onSortChange}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: /name/i }));
    expect(onSortChange).toHaveBeenCalledWith("name", "desc");
  });

  it("renders empty content when rows is empty", () => {
    render(
      <PanelDataTable<Row>
        rows={[]}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        empty={<div>nothing here</div>}
      />
    );
    expect(screen.getByText("nothing here")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/frontend && pnpm test PanelDataTable -- --run
```

Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```tsx
// apps/frontend/src/components/teams/shell/PanelDataTable.tsx
"use client";

import type { ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface PanelDataColumn<Row> {
  key: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  width?: string;
}

export type PanelDataSortDir = "asc" | "desc";

interface PanelDataTableProps<Row> {
  rows: Row[];
  columns: PanelDataColumn<Row>[];
  rowKey: (row: Row) => string;
  onRowClick?: (row: Row) => void;
  sortKey?: string;
  sortDir?: PanelDataSortDir;
  onSortChange?: (key: string, dir: PanelDataSortDir) => void;
  empty?: ReactNode;
}

export function PanelDataTable<Row>({
  rows,
  columns,
  rowKey,
  onRowClick,
  sortKey,
  sortDir,
  onSortChange,
  empty,
}: PanelDataTableProps<Row>) {
  if (rows.length === 0 && empty) {
    return <div className="px-3 py-2">{empty}</div>;
  }

  function toggleSort(col: PanelDataColumn<Row>) {
    if (!col.sortable || !onSortChange) return;
    const nextDir: PanelDataSortDir =
      sortKey === col.key && sortDir === "asc" ? "desc" : "asc";
    onSortChange(col.key, nextDir);
  }

  return (
    <Table>
      <TableHeader>
        <TableRow style={{ borderBottom: "1px solid var(--edge)" }}>
          {columns.map((col) => (
            <TableHead
              key={col.key}
              style={{ width: col.width }}
              className={cn(
                "text-[10px] font-semibold uppercase tracking-[0.12em]",
                col.align === "right" && "text-right",
                col.align === "center" && "text-center",
              )}
            >
              {col.sortable && onSortChange ? (
                <button
                  type="button"
                  onClick={() => toggleSort(col)}
                  className="inline-flex items-center gap-1 transition-colors hover:text-[var(--ink)]"
                  style={{ color: "var(--mute)" }}
                >
                  {col.header}
                  {sortKey === col.key &&
                    (sortDir === "asc" ? (
                      <ChevronUp className="h-3 w-3" />
                    ) : (
                      <ChevronDown className="h-3 w-3" />
                    ))}
                </button>
              ) : (
                <span style={{ color: "var(--mute)" }}>{col.header}</span>
              )}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow
            key={rowKey(row)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            className={cn(
              "transition-colors",
              onRowClick && "cursor-pointer hover:bg-[var(--edge)]",
            )}
            style={{ borderBottom: "1px solid var(--edge)" }}
          >
            {columns.map((col) => (
              <TableCell
                key={col.key}
                className={cn(
                  "text-[13px]",
                  col.align === "right" && "text-right",
                  col.align === "center" && "text-center",
                )}
                style={{ color: "var(--ink)" }}
              >
                {col.cell(row)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
```

- [ ] **Step 4: Run test, verify pass**

```sh
cd apps/frontend && pnpm test PanelDataTable -- --run
```

Expected: PASS, 3 assertions.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/components/teams/shell/PanelDataTable.tsx \
         apps/frontend/tests/unit/components/teams/shell/PanelDataTable.test.tsx
git commit -m "feat(teams-shell): PanelDataTable — generic typed table with sortable headers"
```

---

## Task 12: Barrel export

Single import surface for consumers.

**Files:**
- Create: `apps/frontend/src/components/teams/shell/index.ts`

- [ ] **Step 1: Write the barrel**

```ts
// apps/frontend/src/components/teams/shell/index.ts
export { EditorialButton } from "./EditorialButton";
export type { EditorialButtonVariant, EditorialButtonSize } from "./EditorialButton";

export { EditorialInput } from "./EditorialInput";

export { EditorialSelect } from "./EditorialSelect";
export type { EditorialSelectOption } from "./EditorialSelect";

export { EditorialDialog } from "./EditorialDialog";
export { EditorialCard } from "./EditorialCard";

export { PanelMasthead } from "./PanelMasthead";
export { PanelToolbar } from "./PanelToolbar";

export { PanelTabs } from "./PanelTabs";
export type { PanelTabDefinition } from "./PanelTabs";

export { PanelSection } from "./PanelSection";
export { PanelEmptyState } from "./PanelEmptyState";

export { PanelDataTable } from "./PanelDataTable";
export type { PanelDataColumn, PanelDataSortDir } from "./PanelDataTable";
```

- [ ] **Step 2: Verify it imports cleanly**

```sh
cd apps/frontend && pnpm tsc --noEmit
```

Expected: no type errors.

- [ ] **Step 3: Verify a smoke import works in a test file**

```sh
cat > /tmp/smoke.test.tsx <<'EOF'
import { describe, it, expect } from "vitest";
import * as shell from "@/components/teams/shell";

describe("teams shell barrel", () => {
  it("exports all atoms", () => {
    expect(shell.EditorialButton).toBeTypeOf("object");
    expect(shell.EditorialInput).toBeTypeOf("object");
    expect(shell.EditorialSelect).toBeTypeOf("function");
    expect(shell.EditorialDialog).toBeTypeOf("function");
    expect(shell.EditorialCard).toBeTypeOf("function");
    expect(shell.PanelMasthead).toBeTypeOf("function");
    expect(shell.PanelToolbar).toBeTypeOf("function");
    expect(shell.PanelTabs).toBeTypeOf("function");
    expect(shell.PanelSection).toBeTypeOf("function");
    expect(shell.PanelEmptyState).toBeTypeOf("function");
    expect(shell.PanelDataTable).toBeTypeOf("function");
  });
});
EOF
cp /tmp/smoke.test.tsx apps/frontend/tests/unit/components/teams/shell/index.test.tsx
cd apps/frontend && pnpm test "shell/index" -- --run
```

Expected: PASS, 1 assertion. (`forwardRef` components serialize as `"object"` not `"function"`, hence the mixed assertion types.)

- [ ] **Step 4: Commit**

```sh
git add apps/frontend/src/components/teams/shell/index.ts \
         apps/frontend/tests/unit/components/teams/shell/index.test.tsx
git commit -m "feat(teams-shell): barrel export"
```

---

## Task 13: Token audit + globals.css cleanup

Confirm only the tokens we need ship in `globals.css`, document each.

**Files:**
- Modify: `apps/frontend/src/app/globals.css`

- [ ] **Step 1: Read current token block**

```sh
grep -n -A 20 -E "^:root|^@theme" apps/frontend/src/app/globals.css | head -60
```

Note which tokens are referenced from the atom files we just created (search): `--cream --forest --ink --mute --edge --warn --danger --forest-tint`. If `--forest-tint` is missing, add it.

- [ ] **Step 2: Add `--forest-tint` token if absent**

If grep showed `--forest-tint` is missing, append to the editorial token block:

```css
/* Add inside the `:root` block alongside existing editorial tokens */
--forest-tint: rgba(6, 64, 43, 0.06); /* hover wash for editorial-card-hover, button hovers */
```

- [ ] **Step 3: Document each editorial token in a comment block above the `:root`**

Insert this comment immediately before the `:root` block:

```css
/*
 * Editorial design tokens (PR #542 + teams-shell atoms).
 * Stable surface contract for `apps/frontend/src/components/teams/shell/*`.
 *   --cream         page background; text on dark surfaces.
 *   --ink           primary text.
 *   --mute          secondary text, overline, tabular labels.
 *   --edge          hairline borders, separators, hover wash on dark text.
 *   --forest        primary brand color; primary CTA fill, focus ring base, active tab underline.
 *   --forest-tint   subtle forest tint for hover states without full forest fill.
 *   --warn          warning text + low-priority danger.
 *   --danger        destructive button fill, error border + helper text.
 */
```

- [ ] **Step 4: Run all atom tests + tsc to make sure nothing broke**

```sh
cd apps/frontend && pnpm tsc --noEmit && pnpm test "components/teams/shell" -- --run
```

Expected: tsc clean; all 11 shell tests + the barrel test pass.

- [ ] **Step 5: Commit**

```sh
git add apps/frontend/src/app/globals.css
git commit -m "feat(teams-shell): document editorial tokens + add --forest-tint"
```

---

## Task 14: Run full frontend test suite + open PR

Final regression check + open the PR for review.

- [ ] **Step 1: Run the full frontend test suite**

```sh
cd apps/frontend && pnpm test
```

Expected: all tests pass. If any pre-existing test breaks, investigate — atom additions should be additive only.

- [ ] **Step 2: Run lint**

```sh
cd apps/frontend && pnpm lint
```

Expected: no errors.

- [ ] **Step 3: Run the type check**

```sh
cd apps/frontend && pnpm tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Push branch**

```sh
git push -u origin spec/teams-port-editorial-atoms
```

- [ ] **Step 5: Open the PR**

```sh
gh pr create --title "feat(teams-shell): editorial atom library (PR 1 of /teams port)" --body "$(cat <<'EOF'
## Summary

First PR of the multi-step /teams panel port (see spec at `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md`). Adds the editorial-tokenized atom layer that subsequent panel-port PRs will compose with.

## What's included

- **11 atoms** in `apps/frontend/src/components/teams/shell/`:
  - Editorial primitives: `EditorialButton`, `EditorialInput`, `EditorialSelect`, `EditorialDialog`, `EditorialCard`
  - Panel scaffolding: `PanelMasthead`, `PanelToolbar`, `PanelTabs`, `PanelSection`, `PanelEmptyState`, `PanelDataTable`
- Barrel export at `shell/index.ts`
- vitest snapshot + behavior tests for every atom
- `globals.css` token audit: documented each editorial token + added `--forest-tint`
- Missing shadcn primitives added (`select`, `tabs`, `table`, `collapsible`)

## What's NOT in this PR

- No panel refactors. Panels still use their existing thin shape.
- No visual lab route (per spec — design review happens on dev once panels start using these in PR 2).

## Test plan

- [x] `pnpm test` (frontend full suite) — all pass
- [x] `pnpm tsc --noEmit` — clean
- [x] `pnpm lint` — clean
- [ ] Manual import smoke (post-merge): use one atom in DashboardPanel.tsx as a sanity check before opening PR 2 (Inbox port).

## Next

PR 2: Inbox port — first real consumer of these atoms; will stress-test the atom design and any v1 issues get resolved before stamping the API.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 6: Commit (if any uncommitted changes from steps 1-3)**

If lint/tsc surfaced fixes that needed committing, do so via:

```sh
git add -u && git commit -m "chore: lint/tsc fixes for editorial atom layer"
git push
```

---

## Self-review notes

**Spec coverage:**
- Atom layer (B) — ✅ Tasks 1-12 implement the 11 atoms + barrel
- Token cleanup — ✅ Task 13
- Visual lab — explicitly skipped per spec resolved-decisions
- Tests per atom — ✅ vitest test in each task
- Per-panel port plan (A) — out of scope for THIS plan; covered by separate plans (PRs 2-10) that follow this one

**Placeholder scan:** No "TBD" / "TODO" / "fill in later" / "similar to" tokens. Every step has actual code.

**Type consistency:**
- `EditorialButtonVariant` / `EditorialButtonSize` declared in Task 1 + re-exported in Task 12 ✓
- `EditorialSelectOption` declared in Task 3 + re-exported in Task 12 ✓
- `PanelTabDefinition` declared in Task 7 + re-exported in Task 12 ✓
- `PanelDataColumn<Row>` / `PanelDataSortDir` declared in Task 11 + re-exported in Task 12 ✓
- `PanelEmptyState` action shape consistent: `{ label, onClick }` in Task 10
- All atoms use `cn(...)` from `@/lib/utils` (existing in the repo)

No gaps detected.
