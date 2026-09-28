declare module "dcv" {
  export interface DcvFileExplorer {
    list(path: string): Promise<unknown>;
    storeFile(file: File, path: string): Promise<void>;
    retrieveFile(path: string): Promise<void>;
  }
  export interface DcvConnection {
    disconnect(): void;
    requestDisplayLayout(layout: unknown[]): void;
    captureClipboardEvents(enabled: boolean, target: Window): void;
    enableDisplayQualityUpdates(enabled: boolean): void;
    setDisplayQuality(display: number, image: number): void;
    getFileExplorer(): Promise<DcvFileExplorer>;
  }
  export interface DcvSdk {
    authenticate(url: string, callbacks: {
      promptCredentials(): void;
      // DCV calls it with the authentication first, then `{ code, message }`.
      error(authentication: unknown, error: unknown): void;
      httpExtraSearchParams(): URLSearchParams;
      success(auth: unknown, result: Array<{ sessionId?: string; authToken?: string }>): void;
    }): void;
    connect(options: Record<string, unknown>): Promise<DcvConnection>;
  }
  const dcv: DcvSdk;
  export default dcv;
}
