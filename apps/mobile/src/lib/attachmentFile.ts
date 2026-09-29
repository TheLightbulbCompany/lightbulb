import type { RoomAttachmentInput } from "@lightbulb/shared";
import { Directory, File, Paths } from "expo-file-system";
import { ImageManipulator, SaveFormat } from "expo-image-manipulator";

const inFlight = new Map<string, Promise<File>>();
const MAX_NAME = 120;

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
  // Bounded: a file name may be 255 bytes and so may an attachment's own name,
  // which leaves no room for the id in front of it. The end is what is kept,
  // since that is where the extension is.
  const name = key.replace(/[^\w.-]/g, "_").slice(-MAX_NAME);
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

/** ".png" and the like, or nothing when the name has no short extension. */
const extension = (fileName: string) => /\.[A-Za-z0-9]{1,8}$/.exec(fileName)?.[0] ?? "";

type AttachmentUrl = (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;

/** One room attachment, from the short-lived link the backend signs for it. */
export function attachmentFile(
  roomId: string,
  attachment: { id: string; fileName: string },
  attachmentUrl: AttachmentUrl,
): Promise<File> {
  // The id last: it is what makes the name unique, so it has to survive the cut.
  return cachedDownload(`${attachment.fileName}_${attachment.id}${extension(attachment.fileName)}`, () =>
    attachmentUrl(roomId, attachment.id));
}

/**
 * A file on the device as an upload part. Expo's `fetch` wants a part that can
 * hand over its bytes; it refuses React Native's `{uri, name, type}`
 * descriptor outright. The name and type are the ones the person's photo
 * carries, not the cache file's.
 */
export function uploadPart(file: { uri: string; name: string; type: string }): RoomAttachmentInput {
  const source = new File(file.uri);
  return { name: file.name, type: file.type, bytes: () => source.bytes() };
}

/** The longest side of a photo that had to be re-encoded. */
const MAX_EDGE = 2048;

/**
 * A photo the messaging endpoint will not take as it is (an iPhone's own
 * HEIC, or anything over the size cap), scaled to fit `MAX_EDGE` and saved as
 * a JPEG. A photo is never scaled up.
 */
export async function asJpeg(photo: {
  uri: string;
  name: string;
  width: number;
  height: number;
}): Promise<{ uri: string; name: string; size: number }> {
  const context = ImageManipulator.manipulate(photo.uri);
  if (Math.max(photo.width, photo.height) > MAX_EDGE) {
    context.resize(photo.width >= photo.height ? { width: MAX_EDGE } : { height: MAX_EDGE });
  }
  const image = await context.renderAsync();
  const saved = await image.saveAsync({ format: SaveFormat.JPEG, compress: 0.8 });
  return {
    uri: saved.uri,
    name: `${photo.name.replace(/\.[^.]+$/, "")}.jpg`,
    size: new File(saved.uri).size ?? 0,
  };
}
