import { Directory, File, Paths } from "expo-file-system";

const inFlight = new Map<string, Promise<File>>();

/**
 * Bytes behind a URL, as a file in the app's cache: downloaded once, then read
 * from there by everything that shows them (the thumbnail in the timeline, the
 * viewer, the share sheet).
 *
 * Straight to disk, never through a `Blob`. This React Native cannot build one
 * from bytes ("Creating blobs from 'ArrayBuffer' and 'ArrayBufferView' are not
 * supported"), and that is what `response.blob()` does here -- so every image
 * in a room drew as a blank tile and opened as "Could not load".
 *
 * `key` names the file, so it has to be unique to the bytes (an attachment id).
 */
export function cachedDownload(
  key: string,
  source: () => Promise<{ url: string; headers?: Record<string, string> }>,
): Promise<File> {
  const name = key.replace(/[^\w.-]/g, "_");
  const pending = inFlight.get(name);
  if (pending) return pending;
  const load = (async () => {
    const dir = new Directory(Paths.cache, "attachments");
    if (!dir.exists) dir.create({ intermediates: true });
    const file = new File(dir, name);
    // An attempt that died mid-write leaves an empty file behind; fetch it again.
    if (file.exists && (file.size ?? 0) > 0) return file;
    const { url, headers } = await source();
    await File.downloadFileAsync(url, file, { idempotent: true, headers });
    return file;
  })().finally(() => inFlight.delete(name));
  inFlight.set(name, load);
  return load;
}

type AttachmentUrl = (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;

/** One room attachment, from the short-lived link the backend signs for it. */
export function attachmentFile(
  roomId: string,
  attachment: { id: string; fileName: string },
  attachmentUrl: AttachmentUrl,
): Promise<File> {
  return cachedDownload(`${attachment.id}_${attachment.fileName}`, () => attachmentUrl(roomId, attachment.id));
}
