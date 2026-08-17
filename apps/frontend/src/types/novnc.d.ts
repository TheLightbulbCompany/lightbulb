// Minimal surface of @novnc/novnc v1.7 (ships no types; the DefinitelyTyped
// package tracks an older API). Only what BrowserDockPanel touches — widen
// here as usage grows rather than pulling a stale @types package.
declare module "@novnc/novnc" {
  export interface NoVncCredentials {
    username?: string;
    password?: string;
    target?: string;
  }

  export default class RFB extends EventTarget {
    constructor(
      target: HTMLElement,
      url: string,
      options?: { credentials?: NoVncCredentials; wsProtocols?: string[] },
    );
    disconnect(): void;
    scaleViewport: boolean;
    resizeSession: boolean;
    background: string;
  }
}
