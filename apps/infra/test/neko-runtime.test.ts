import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parse as parseIni } from "ini";
import { describe, expect, test } from "vitest";
import { parseDocument } from "yaml";

const browserPath = (file: string) => fileURLToPath(new URL(`../openclaw/browser/${file}`, import.meta.url));

function parseYaml(file: string): Record<string, unknown> {
  const document = parseDocument(readFileSync(file, "utf8"));
  if (document.errors.length > 0) {
    throw new Error(document.errors.map((error) => error.message).join("\\n"));
  }
  return document.toJS() as Record<string, unknown>;
}

function parseSupervisor(file: string): Record<string, Record<string, unknown>> {
  return parseIni(readFileSync(file, "utf8")) as Record<string, Record<string, unknown>>;
}

describe("pinned Neko runtime", () => {
  test("keeps the owner task's interactive WebRTC contract", () => {
    const config = parseYaml(browserPath("neko.yaml"));
    const supervisor = parseSupervisor(browserPath("browser-supervisord.conf"));
    const server = config.server as Record<string, unknown>;
    const desktop = config.desktop as Record<string, unknown>;
    const input = desktop.input as Record<string, unknown>;
    const capture = config.capture as Record<string, Record<string, unknown>>;
    const video = capture.video as Record<string, unknown>;
    const pipelines = video.pipelines as Record<string, Record<string, unknown>>;
    const webrtc = config.webrtc as Record<string, unknown>;

    expect(server).toMatchObject({
      bind: "0.0.0.0:6081",
      static: "/var/www/neko",
      path_prefix: "/api/v1/browser-view/webrtc",
      proxy: true,
      pprof: false,
      metrics: false,
    });
    expect(config.member).toEqual({ provider: "noauth" });
    expect(config.session).toMatchObject({ implicit_hosting: true, merciful_reconnect: true });
    expect((config.session as Record<string, Record<string, unknown>>).cookie).toMatchObject({
      enabled: true,
      expiration: "15m",
      secure: true,
      http_only: true,
      path: "/api/v1/browser-view/webrtc",
    });
    expect(desktop).toMatchObject({ display: ":1", upload_drop: false, file_chooser_dialog: false });
    expect(input.enabled).toBe(false);
    expect(capture.screencast?.enabled).toBe(false);
    expect(capture.webcam?.enabled).toBe(false);
    expect(capture.microphone?.enabled).toBe(false);
    expect(capture.broadcast).toMatchObject({ url: "", autostart: false });
    expect(video).toMatchObject({ codec: "vp8", ids: ["browser"] });
    expect(pipelines.browser).toMatchObject({ fps: 24, gst_encoder: "vp8enc" });
    expect((pipelines.browser.gst_params as Record<string, unknown>)["target-bitrate"]).toBe(1_800_000);
    expect(capture.audio).toMatchObject({ codec: "opus", device: "audio_output.monitor" });
    expect(webrtc).toMatchObject({ icelite: true, icetrickle: true, udpmux: 59000, tcpmux: 59000 });
    expect(Object.keys(supervisor).filter((section) => section.startsWith("program:"))).toEqual([
      "program:pulseaudio",
      "program:openbox",
      "program:neko",
    ]);
    expect(supervisor["program:neko"].command).toContain("/usr/local/bin/neko serve");
    expect(supervisor["program:neko"].command).not.toMatch(/chrome|chromium/i);
    expect(supervisor["program:pulseaudio"].command).toContain("/usr/bin/pulseaudio");
    expect(supervisor["program:openbox"].command).toContain("/usr/bin/openbox");
    for (const section of ["program:pulseaudio", "program:openbox", "program:neko"]) {
      expect(supervisor[section].autorestart).toBe(true);
    }
  });
});
