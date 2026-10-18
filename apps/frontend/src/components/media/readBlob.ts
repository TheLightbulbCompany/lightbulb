// FileReader rather than Blob.text()/arrayBuffer(): jsdom's Blob has neither, and every browser has this.
const read = <T extends string | ArrayBuffer>(go: (r: FileReader) => void) =>
  new Promise<T>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result as T);
    r.onerror = () => reject(r.error);
    go(r);
  });

export const blobText = (b: Blob) => read<string>((r) => r.readAsText(b));
export const blobBytes = (b: Blob) => read<ArrayBuffer>((r) => r.readAsArrayBuffer(b));
