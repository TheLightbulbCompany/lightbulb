"""Attach this terminal to the held session as the interactive client. Detach: close the window or Ctrl-\\."""
import json, os, select, signal, socket, struct, sys, termios, tty
SOCK = "/private/tmp/lbh-manual.sock"
s = socket.socket(socket.AF_UNIX); s.connect(SOCK)
def send(t, p=b""): s.sendall(bytes([t]) + struct.pack(">I", len(p)) + p)
def size():
    c, r = os.get_terminal_size(); send(4, struct.pack(">HH", c, r))
send(1, json.dumps({"role": "interactive"}).encode())
old = termios.tcgetattr(0); tty.setraw(0)
signal.signal(signal.SIGWINCH, lambda *_: size()); size()
buf = b""
try:
    while True:
        r, _, _ = select.select([0, s], [], [])
        if 0 in r:
            d = os.read(0, 65536)
            if not d or d == b"\x1c": break
            send(3, d)
        if s in r:
            d = s.recv(65536)
            if not d: break
            buf += d
            while len(buf) >= 5:
                t, n = buf[0], struct.unpack(">I", buf[1:5])[0]
                if len(buf) < 5 + n: break
                p = buf[5:5 + n]; buf = buf[5 + n:]
                if t in (2, 8): os.write(1, p[8:])
                elif t == 9: raise SystemExit("\r\n[another window took this session]\r\n")
                elif t == 5: raise SystemExit("\r\n[session ended]\r\n")
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
