import * as FileSystem from "expo-file-system";

import { attachmentFile, cachedDownload, uploadPart } from "../attachmentFile";

const files = (FileSystem as unknown as { __files: Map<string, string> }).__files;
const link = () => jest.fn(async () => ({ url: "https://s3.example/a.png?sig=x", expiresIn: 900 }));

beforeEach(() => files.clear());

describe("attachmentFile", () => {
  it("downloads once and reads the cached file after that", async () => {
    const attachmentUrl = link();
    const first = await attachmentFile("room-1", { id: "att-1", fileName: "a.png" }, attachmentUrl);
    const again = await attachmentFile("room-1", { id: "att-1", fileName: "a.png" }, attachmentUrl);
    expect(again.uri).toBe(first.uri);
    expect(attachmentUrl).toHaveBeenCalledTimes(1);
  });

  it("shares one download between callers that ask at the same time", async () => {
    const attachmentUrl = link();
    const asked = { id: "att-2", fileName: "b.png" };
    await Promise.all([attachmentFile("room-1", asked, attachmentUrl), attachmentFile("room-1", asked, attachmentUrl)]);
    expect(attachmentUrl).toHaveBeenCalledTimes(1);
  });

  it("names the file safely whatever the attachment is called", async () => {
    const file = await attachmentFile("room-1", { id: "att-3", fileName: "Screenshot at 2.10.41\u202fPM.png" }, link());
    expect(file.uri).toMatch(/attachments\/Screenshot_at_2\.10\.41_PM\.png_att-3\.png$/);
  });

  // The backend takes names up to 255 bytes; so does a file name, id included.
  it("keeps a long name within what a file name may be, and still unique", async () => {
    const long = `${"a".repeat(250)}.png`;
    const one = await attachmentFile("room-1", { id: "11111111-1111-4111-8111-111111111111", fileName: long }, link());
    const two = await attachmentFile("room-1", { id: "22222222-2222-4222-8222-222222222222", fileName: long }, link());
    const name = (uri: string) => uri.slice(uri.lastIndexOf("/") + 1);
    expect(name(one.uri).length).toBeLessThanOrEqual(120);
    expect(name(one.uri)).toMatch(/11111111-1111-4111-8111-111111111111\.png$/);
    expect(one.uri).not.toBe(two.uri);
  });

  it("fetches again when an earlier attempt left an empty file", async () => {
    const attachmentUrl = link();
    const file = await attachmentFile("room-1", { id: "att-4", fileName: "c.png" }, attachmentUrl);
    files.set(file.uri, "");
    await attachmentFile("room-1", { id: "att-4", fileName: "c.png" }, attachmentUrl);
    expect(attachmentUrl).toHaveBeenCalledTimes(2);
  });

  it("asks again after a failure instead of remembering it", async () => {
    const source = jest.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ url: "https://s3.example/d.png" });
    await expect(cachedDownload("att-5", source)).rejects.toThrow("offline");
    await expect(cachedDownload("att-5", source)).resolves.toBeTruthy();
  });
});

// Expo's `fetch` refuses React Native's `{uri}` file descriptor, so a part has
// to carry its own bytes: that refusal failed every photo sent from the phone.
describe("uploadPart", () => {
  it("hands over the file's bytes under the photo's own name and type", async () => {
    files.set("file:///picked/1549BA7.jpeg", "jpeg-bytes");
    const part = uploadPart({ uri: "file:///picked/1549BA7.jpeg", name: "beach.jpeg", type: "image/jpeg" });
    expect(part).toMatchObject({ name: "beach.jpeg", type: "image/jpeg" });
    expect("uri" in part).toBe(false);
    expect(new TextDecoder().decode(await (part as { bytes(): Promise<Uint8Array> }).bytes())).toBe(
      "jpeg-bytes",
    );
  });
});
