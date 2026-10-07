"use client";

import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export type EditorialButtonVariant = "primary" | "secondary" | "ghost" | "destructive";
export type EditorialButtonSize = "sm" | "md" | "lg" | "icon";

export interface EditorialButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
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
          `editorial-btn--${size}`,
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
