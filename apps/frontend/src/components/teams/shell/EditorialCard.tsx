import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export interface EditorialCardProps extends HTMLAttributes<HTMLDivElement> {
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
