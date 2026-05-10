"use client";

import useSWR from "swr";

import { useApi } from "@/lib/api";

type HookCredentials = {
  public_url: string;
  token: string;
  rotated_at: string | null;
};

export function useHookCredentials() {
  const { get } = useApi();
  const { data, error, isLoading, mutate } = useSWR<HookCredentials>(
    "/hooks/credentials",
    (url: string) => get(url) as Promise<HookCredentials>,
  );
  return { credentials: data, error, isLoading, mutate };
}
