"use client";

// SWR hooks over the owner brain routes (routers/brain.py) — the memory-wiki
// vault the agents read and write on EFS. Shapes mirror useWorkspaceFiles.ts
// exactly so FileTree/FileContentViewer plug in unchanged; paths are relative
// to the wiki vault root.

import * as React from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import type { FileEntry, FileInfo } from "@/hooks/useWorkspaceFiles";

export function useBrainTree() {
  const api = useApi();
  const key = "/container/brain/tree";

  const { data, error, isLoading, mutate } = useSWR<{ files: FileEntry[] }>(
    key,
    () => api.get(key) as Promise<{ files: FileEntry[] }>,
  );

  return {
    files: data?.files ?? [],
    error,
    isLoading,
    refresh: mutate,
  };
}

export function useBrainFile(filePath: string | null) {
  const api = useApi();
  const key = filePath
    ? `/container/brain/file?path=${encodeURIComponent(filePath)}`
    : null;

  const { data, error, isLoading } = useSWR<FileInfo>(
    key,
    () => api.get(key!) as Promise<FileInfo>,
  );

  return {
    file: data ?? null,
    error,
    isLoading,
    // Exposed so a caller (BrainPanel, after a save) can mutate this exact
    // SWR cache entry via useSWRConfig() without re-deriving the query string.
    key,
  };
}

export function useBrainMutations() {
  const api = useApi();
  return React.useMemo(
    () => ({
      saveBrainFile: (path: string, content: string) =>
        api.put("/container/brain/file", { path, content }) as Promise<{
          path: string;
          size: number;
          modified_at: number;
        }>,
      deleteBrainFile: (path: string) =>
        api.del(`/container/brain/file?path=${encodeURIComponent(path)}`),
    }),
    [api],
  );
}
