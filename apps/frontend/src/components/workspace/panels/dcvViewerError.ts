import { captureException } from "@/lib/analytics";

export function viewerDiagnostic(stage: string, reason: unknown): Error {
  const detail = reason && typeof reason === "object"
    ? reason as { name?: unknown; message?: unknown; stack?: unknown }
    : { message: reason };
  // SDK exceptions can include presigned AWS URLs. Preserve the exception and
  // stage, but never send those bearer credentials to console or telemetry.
  const redact = (value: unknown) => String(value ?? "")
    .replace(/(?:https?|wss?):\/\/[^\s"'<>)]*/gi, "[redacted URL]")
    .slice(0, 4000);
  const error = new Error(`Browser viewer ${stage}: ${redact(detail.message)}`);
  error.name = redact(detail.name) || "BrowserViewerError";
  if (detail.stack) error.stack = `${error.name}: ${error.message}\n${redact(detail.stack)}`;
  return error;
}

export function reportViewerError(stage: string, reason: unknown): void {
  const error = viewerDiagnostic(stage, reason);
  console.error(error);
  captureException(error);
}
