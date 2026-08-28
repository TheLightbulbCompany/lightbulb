"use client";

import { forwardRef, useId } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface EditorialInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "id"> {
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
        <label
          htmlFor={inputId}
          className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--midnight-muted)]"
        >
          {label}
        </label>
        <input
          id={inputId}
          ref={ref}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={cn(helpText && !error && helpId, error && errorId) || undefined}
          className={cn(
            "rounded-md border px-3 py-2 text-[13px] outline-none transition-colors",
            "border-[var(--edge)]",
            "focus:ring-2 focus:ring-[var(--midnight)]/20 focus:border-[var(--midnight)]",
            error && "border-[var(--err)] focus:ring-[var(--err)]/20 focus:border-[var(--err)]",
            "bg-[var(--cream)] text-[var(--midnight)]",
            className,
          )}
          {...rest}
        />
        {helpText && !error && (
          <p id={helpId} className="text-[11px] text-[var(--midnight-muted)]">
            {helpText}
          </p>
        )}
        {error && (
          <p id={errorId} role="alert" className="text-[11px] text-[var(--err)]">
            {error}
          </p>
        )}
      </div>
    );
  },
);

EditorialInput.displayName = "EditorialInput";
