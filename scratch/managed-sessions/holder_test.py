"""Drive the spike holder like the app would: start it detached, attach late, reattach mid-turn, paste, Esc."""
import json, os, re, signal, socket, struct, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); HOME = os.environ["HOME"]
SOCK = f"/private/tmp/lbh-spike-{os.getpid()}.sock"; LOGS = HERE + "/logs"
HOLDER = HERE + "/holder/target/release/lb-holder-spike"
env = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "SHELL", "TMPDIR") if k in os.environ}  # like a GUI app: no TERM, no LANG

def frames(s, timeout):
    """Yield (type, payload) until `timeout` seconds pass with nothing new."""
    s.settimeout(timeout); buf = b""
    while True:
        try: d = s.recv(65536)
        except socket.timeout: return
        if not d: return
        buf += d
        while len(buf) >= 5:
            t, n = buf[0], struct.unpack(">I", buf[1:5])[0]
            if len(buf) < 5 + n: break
            yield t, buf[5:5 + n]; buf = buf[5 + n:]

def send(s, t, payload=b""): s.sendall(bytes([t]) + struct.pack(">I", len(payload)) + payload)
def text(b): return " ".join(re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[a-zA-Z~]|\x1b[()][A-Z0-9]|\x1b[=>78]", " ", b.decode("utf8", "ignore")).split())

def attach(role="interactive", settle=3.0):
    s = socket.socket(socket.AF_UNIX); s.connect(SOCK)
    send(s, 1, json.dumps({"role": role}).encode())
    hello = snap = None; out = b""
    for t, p in frames(s, settle):
        if t == 1: hello = json.loads(p)
        elif t == 8: snap = p[8:]
        elif t == 2: out += p[8:]
    return s, hello, snap, out

def drain(s, secs):
    out = b""
    for t, p in frames(s, secs):
        if t == 2: out += p[8:]
    return out

def typed(s, txt):
    for ch in txt: send(s, 3, ch.encode()); time.sleep(0.01)
    time.sleep(0.6); send(s, 3, b"\r")

def status(pid):
    try: return json.load(open(f"{HOME}/.claude/sessions/{pid}.json")).get("status")
    except Exception as e: return f"? {type(e).__name__}"

facts = {}
h = subprocess.Popen([HOLDER, SOCK, LOGS, "--", "claude", "--model", "haiku"],
                     cwd=HERE + "/work", env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=open(LOGS + ".stderr", "w") if os.path.isdir(LOGS) or not os.makedirs(LOGS) else None)
time.sleep(15)  # (a) nobody attached while Claude starts
s, hello, snap, _ = attach()
pid = hello["providerPid"]; facts["provider_pid"] = pid
facts["a_started_without_client"] = "Claude Code" in text(snap)
facts["a_snapshot_has_bracketed_paste"] = b"\x1b[?2004h" in snap
facts["a_status_after_start"] = status(pid)
facts["a_unknown_probes"] = open(LOGS + "/probes.log").read().splitlines()[:12]
if "trust this folder" in text(snap):  # Claude's normal first-run question for a new folder
    send(s, 3, b"\x1b[B"); time.sleep(0.4); send(s, 3, b"\r")
    facts["a_after_trust"] = text(drain(s, 8))[-160:]
# (b) a turn, then the client dies mid-turn and a new one attaches
typed(s, "Write the numbers from 1 to 300, one per line, nothing else.")
time.sleep(4); s.close()
time.sleep(3)
s, hello2, snap, _ = attach()
facts["b_same_provider_after_reattach"] = hello2["providerPid"] == pid and os.path.exists(f"/proc/{pid}") or subprocess.run(["kill", "-0", str(pid)]).returncode == 0
facts["b_snapshot_shows_numbers"] = bool(re.search(r"\b(1\d\d|2\d\d)\b", text(snap)))
drain(s, 20)
# (c) a multi-line bracketed paste at idle must not submit
facts["c_status_before_paste"] = status(pid)
send(s, 3, b"\x1b[200~first line\nsecond line\nthird line\x1b[201~")
after = text(drain(s, 3))
facts["c_status_after_paste"] = status(pid)
facts["c_paste_screen"] = after[-160:]
send(s, 3, b"\x15"); drain(s, 1)  # Ctrl-U clears the draft
# (d) Esc during a running turn
typed(s, "Write the numbers from 1 to 500, one per line, nothing else.")
drain(s, 4)
send(s, 3, b"\x1b"); out = text(drain(s, 4))
facts["d_esc_interrupted"] = "nterrupted" in out; facts["d_status_after_esc"] = status(pid)
# (e) Esc with a permission prompt open
typed(s, "Use the Write tool to create a file named esc-test.txt containing the word hi.")
out = text(drain(s, 12)); facts["e_prompt_shown"] = "Do you want" in out or "proceed" in out.lower()
send(s, 3, b"\x1b"); out = text(drain(s, 4))
facts["e_after_esc"] = out[-200:]; facts["e_file_created"] = os.path.exists(HERE + "/work/esc-test.txt")
# end the session
send(s, 3, b"\x15"); typed(s, "/exit"); drain(s, 4)
try: h.wait(timeout=10)
except Exception: os.killpg(h.pid, signal.SIGTERM)
facts["holder_exit_code"] = h.returncode
rows = [l.split("\t") for l in open(LOGS + "/volume.tsv")]
facts["volume_total_bytes"] = sum(int(b) for _, b in rows); facts["volume_secs"] = round(int(rows[-1][0]) / 1000)
print(json.dumps(facts, indent=1))
