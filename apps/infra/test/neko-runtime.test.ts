import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";

const browserPath = (file: string) => fileURLToPath(new URL(`../openclaw/browser/${file}`, import.meta.url));

function parseYaml(file: string): Record<string, unknown> {
  return JSON.parse(execFileSync("ruby", ["-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", file], { encoding: "utf8" }));
}

function parseSupervisor(file: string): Record<string, Record<string, string>> {
  const script = [
    "import configparser, json, sys",
    "config = configparser.ConfigParser()",
    "config.read(sys.argv[1])",
    "print(json.dumps({section: dict(config[section]) for section in config.sections()}))",
  ].join("; ");
  return JSON.parse(execFileSync("python3", ["-c", script, file], { encoding: "utf8" }));
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
    expect(input.enabled).toBe(true);
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
      expect(supervisor[section].autorestart).toBe("true");
    }
  });
});
