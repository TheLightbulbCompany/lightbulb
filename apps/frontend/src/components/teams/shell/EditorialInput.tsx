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
          className="label-caps"
        >
          {label}
        </label>
        <input
          id={inputId}
          ref={ref}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={cn(helpText && !error && helpId, error && errorId) || undefined}
          className={cn(
            "rounded-md border px-3 py-2 text-ui transition-colors",
            "border-[var(--edge)]",
            "",
            error && "border-[var(--err)]",
            "bg-[var(--cream)] text-[var(--midnight)]",
            className,
          )}
          {...rest}
        />
        {helpText && !error && (
          <p id={helpId} className="text-caption text-[var(--midnight-muted)]">
            {helpText}
          </p>
        )}
        {error && (
          <p id={errorId} role="alert" className="text-caption text-[var(--err)]">
            {error}
          </p>
        )}
      </div>
    );
  },
);

EditorialInput.displayName = "EditorialInput";
