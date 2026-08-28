"use client";

import * as React from "react";
import { Loader2, Search } from "lucide-react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface UserSearchInputProps {
  /** Initial input value (uncontrolled). */
  defaultValue?: string;
  /** Fired 300ms after the user stops typing. */
  onSearch: (query: string) => void;
  /** Optional spinner toggle controlled by the caller (e.g. while a fetch is in flight). */
  isLoading?: boolean;
  placeholder?: string;
  className?: string;
}

const DEBOUNCE_MS = 300;

/**
 * Debounced search input for the admin user directory. The component owns
 * the input value; the caller owns the loading state for any in-flight fetch
 * triggered by `onSearch`.
 */
export function UserSearchInput({
  defaultValue = "",
  onSearch,
  isLoading = false,
  placeholder = "Search by email, user ID, or org\u2026",
  className,
}: UserSearchInputProps) {
  const [value, setValue] = React.useState(defaultValue);
  const onSearchRef = React.useRef(onSearch);

  // Keep the latest callback without retriggering the debounce effect.
  React.useEffect(() => {
    onSearchRef.current = onSearch;
  }, [onSearch]);

  React.useEffect(() => {
    const handle = setTimeout(() => {
      onSearchRef.current(value);
    }, DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [value]);

  return (
    <div className={cn("relative w-full", className)}>
      <Search
        className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <Input
        type="search"
        role="searchbox"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={placeholder}
        className="pl-9 pr-10"
        aria-label="Search users"
      />
      {isLoading ? (
        <span
          className="absolute right-3 top-1/2 flex -translate-y-1/2 items-center gap-1 text-xs text-muted-foreground"
          role="status"
          aria-live="polite"
        >
          <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          {"Searching\u2026"}
        </span>
      ) : null}
    </div>
  );
}
