"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { UserSearchInput } from "@/components/admin/UserSearchInput";

export interface UsersClientHeaderProps {
  defaultValue?: string;
}

/**
 * Thin "use client" wrapper that owns the debounced search callback for the
 * users directory. Lives next to the parent Server Component so the page
 * itself can stay server-rendered. On every debounced change it pushes a new
 * URL — server-rendered table re-fetches with the new query.
 */
export function UsersClientHeader({ defaultValue }: UsersClientHeaderProps) {
  const router = useRouter();
  const [isPending, startTransition] = React.useTransition();
  const lastQueryRef = React.useRef(defaultValue ?? "");

  const handleSearch = React.useCallback(
    (query: string) => {
      const trimmed = query.trim();
      if (trimmed === lastQueryRef.current) return;
      lastQueryRef.current = trimmed;
      const target = trimmed
        ? `/admin/users?q=${encodeURIComponent(trimmed)}`
        : "/admin/users";
      startTransition(() => {
        router.push(target);
      });
    },
    [router],
  );

  return (
    <UserSearchInput
      defaultValue={defaultValue}
      onSearch={handleSearch}
      isLoading={isPending}
    />
  );
}
