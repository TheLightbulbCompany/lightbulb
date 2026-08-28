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

export interface EditorialSelectProps {
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
          "border-[var(--edge)] bg-[var(--cream)] text-[var(--midnight)]",
          "focus:ring-[var(--midnight)]/20 focus:border-[var(--midnight)]",
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
            className="text-[var(--midnight)] focus:bg-[var(--edge)] focus:text-[var(--midnight)]"
          >
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
