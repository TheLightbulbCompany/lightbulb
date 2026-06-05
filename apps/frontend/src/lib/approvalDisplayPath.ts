// apps/frontend/src/lib/approvalDisplayPath.ts
//
// Compact an absolute path under a user home directory to a ~-prefixed
// form for display in approval cards. Ported from upstream OpenClaw
// (~/Desktop/openclaw/src/infra/approval-display-paths.ts). WHY: raw
// /Users/<name>/… paths are noisy and leak the OS username; ~/… is the
// same affordance the native control UI shows.

export function formatApprovalDisplayPath(value: string): string {
  const normalized = value.trim();
  if (!normalized || hasRelativePathSegment(normalized)) {
    return normalized;
  }

  const unixHomeMatch = normalized.match(/^\/(?:home|Users)\/([^/]+)(.*)$/);
  if (unixHomeMatch && isSafeHomeSegment(unixHomeMatch[1])) {
    return compactHomeSuffix(unixHomeMatch[2] ?? "");
  }

  const windowsHomeMatch = normalized.match(/^[A-Za-z]:[\\/]Users[\\/]([^\\/]+)(.*)$/i);
  if (windowsHomeMatch && isSafeHomeSegment(windowsHomeMatch[1])) {
    return compactHomeSuffix(windowsHomeMatch[2] ?? "");
  }

  return normalized;
}

function compactHomeSuffix(suffix: string): string {
  return `~${suffix.replace(/\\/g, "/")}`;
}

function isSafeHomeSegment(segment: string | undefined): boolean {
  return segment !== undefined && segment !== "." && segment !== "..";
}

function hasRelativePathSegment(value: string): boolean {
  return /(^|[\\/])\.{1,2}(?=[\\/]|$)/.test(value);
}
