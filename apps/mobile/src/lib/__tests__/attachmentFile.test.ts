import * as FileSystem from "expo-file-system";

import { attachmentFile, cachedDownload } from "../attachmentFile";

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
    const file = await attachmentFile("room-1", { id: "att-3", fileName: "Screenshot at 2.10.41 PM.png" }, link());
    expect(file.uri).toMatch(/attachments\/att-3_Screenshot_at_2\.10\.41_PM\.png$/);
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
