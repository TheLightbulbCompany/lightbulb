import {
  buildUploadNotice,
  filterOversized,
  MAX_ATTACHMENT_SIZE,
  type PendingAttachment,
} from "../attachments";

function att(name: string, size: number): PendingAttachment {
  return { id: name, name, size, uri: `file:///tmp/${name}`, mimeType: "x/y" };
}

describe("filterOversized", () => {
  it("passes files at the cap and unknown (0) sizes", () => {
    const { valid, error } = filterOversized([
      att("ok.bin", MAX_ATTACHMENT_SIZE),
      att("unknown.bin", 0),
    ]);
    expect(valid).toHaveLength(2);
    expect(error).toBeNull();
  });

  it("pluralizes the web's rejection message", () => {
    const { valid, error } = filterOversized([
      att("a.zip", MAX_ATTACHMENT_SIZE + 1),
      att("b.zip", MAX_ATTACHMENT_SIZE + 1),
      att("ok.png", 10),
    ]);
    expect(valid.map((v) => v.name)).toEqual(["ok.png"]);
    expect(error).toBe("a.zip, b.zip exceed the 10MB limit");
  });
});

describe("buildUploadNotice", () => {
  it("emits the web's notice format verbatim", () => {
    expect(
      buildUploadNotice([
        { filename: "a.png", path: ".openclaw/workspaces/x/uploads/a.png", size: 1 },
        { filename: "b.md", path: ".openclaw/workspaces/x/uploads/b.md", size: 2 },
      ]),
    ).toBe(
      "[The user uploaded files to your workspace. You can read them at these paths:\n" +
        "- a.png → .openclaw/workspaces/x/uploads/a.png\n" +
        "- b.md → .openclaw/workspaces/x/uploads/b.md]\n\n",
    );
  });
});
