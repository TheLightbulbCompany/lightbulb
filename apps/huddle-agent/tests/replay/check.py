# ruff: noqa: E741
# Vendored from the live Meet voice rig (.scratch-meet-voice/check.py): the same checks judge a live call and
# a replayed one. One difference, to carry back to the rig (`PIECE`, four uses): the worker now records what
# was audible of a cut sentence as a marked piece ("... [cut off]") and says the sentence again whole. A
# piece is a line (silent_between can name the audio, no_text still sees the cut), and it is not a sentence
# that was said: it is no answer (answer_contains), no repeat of the whole sentence that follows it
# (said_once), and no line of its own to count (max_agent_lines).
#!/usr/bin/env python3
"""Pass/fail check of ONE rig run against the scenario's "expect" block.

usage: check.py <scenario.json> [--rig rig_stdout.txt] [--worker worker.log] [--transcript transcript.json]
                [--pick N] [--strict] [-q]

Three evidence sources, merged on one clock (epoch seconds, shown as seconds on the rig clock):
  rig stdout   wall-clock HH:MM:SS.mmm UTC per event: who spoke when, agent audio bursts   (ms, this Mac's clock)
  worker log   EMF metric lines with _aws.Timestamp (epoch ms): requests created/ended...  (ms, this Mac's clock)
  transcript   the `aws logs filter-log-events ... --query 'events[].message'` JSON (or its text);
               lines `[n @epoch_s] Name( (Meet))?( → to)?: text`                            (1 s, worker's clock)
Any source may be absent: an assertion that needs it prints SKIP with the reason. Exit 1 if any FAIL
(with --strict also 2 if any SKIP).

"expect" is a list of {"<assertion>": {args}, "why": "..."}. A line is a scenario line index. Text args are
case-insensitive regexes (a plain substring works).
  silent_between  {from, to, allow?, allow_max?=1}   no agent speech STARTS between line `from` and line `to`
                  (start edges; "from_edge"/"to_edge": "end" to change; "to" omitted = end of run;
                  or "window": [s0, s1] in scenario seconds, needs rig stdout). `allow`: the owed answer;
                  up to allow_max agent lines matching ALL of these may be said, only at a pause.
  no_new_run      {line | lines, within?=10}           no live.request.created attributable to the line
  one_run         {line, within?=10}                   exactly one
  ack_within      {line, seconds}                     agent audio starts within N s after the line ends
  answer_contains {after, contains, before?}          an agent line after line `after` (and started before
                                                      line `before`) matches all of `contains`
  said_once       {text?, max_overlap_words?=6}       `text` is in at most one agent sentence; with no text:
                  no completed sentence is said twice. A CUT sentence (no end punctuation) may be re-said
                  when it resumes, if the cut part was at most max_overlap_words long.
  stops_within    {line, seconds}                     the person of `line` starts over agent audio: no agent
                                                      audio from N s after their start until they finish
  nothing_after   {line, grace?=3}                    no agent audio or transcript line after the line ends
  max_agent_lines {max, from?, to?}                   at most N agent transcript lines (in the window)
  no_text         {pattern}                           no agent transcript line matches
Added 10-07 for the wider scenario set (answer_contains, max_agent_lines, no_text, min_sentences and
max_words also take "who": a regex on the agent's name; silent_between/max_* windows take "to_plus": seconds
added to the `to` bound, e.g. {"to": 5, "to_edge": "end", "to_plus": 3.5} = until 3.5 s after line 5 ends):
  no_agent_overlap {max?=0.3}                         two different agents never speak at the same time
                                                      (audio per agent track; transcript lines as a second look)
  no_talk_over    {max?=1.0, except?, from?, to?}     no person's line has more than N s of agent audio on it
                                                      (`except`: lines where an overlap is the test itself)
  min_sentences   {after, n, before?}                 the agent says at least N COMPLETE sentences after line
                                                      `after` starts (the acknowledgement counts as one)
  resumes_after   {line, within?}                     the line cut the agent off: agent audio starts again
                                                      after the line ends (within N s)
  keeps_speaking  {line, cover?=0.6, plus?=2}         the agent was speaking when the line started and went on:
                                                      agent audio fills >= cover of [line start, line end + plus]
  in_order        {texts}                             each text is said, and first said in this order
  max_runs        {max, from?, to?}                   at most N live.request.created (in the window)
  max_agents      {max}                               at most N different agents have a transcript line
  max_words       {max, from?, to?, allow?}           agent lines in the window, not counting those that match
                                                      `allow` (the owed answer), have at most N words in total
"""
import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys

# Measured 10-07 on three runs with all three sources: a PERSON's transcript mark is the STT final,
# 1.3-2.2 s after the rig finished playing the line (floored to 1 s). An AGENT's mark is the end of the
# utterance, -0.2..+1.5 s. Used only when rig stdout is absent, or to tie a transcript line to a burst.
MARK_LAG = 1.4
WORDS_PER_S = 2.7  # speech rate, to estimate a start from an end
RIG_LINE = re.compile(r"^\s*(-?\d+\.\d+)\s+(\d\d):(\d\d):(\d\d)\.(\d{3})\s+([A-Z_]+)\s*(.*)$")
PIECE = re.compile(r"\[cut off\]\s*$")  # huddle.CUT_MARK
TR_LINE = re.compile(r"^\[(\d+) @(\d+)\] (.+?)( \(Meet\))?(?: → (.+?))?: ?(.*)$")


def words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9$']+", s.lower().replace("’", "'"))


def hit(patterns, text: str) -> bool:
    return all(re.search(p, text, re.I) for p in ([patterns] if isinstance(patterns, str) else patterns))


def merge(raw: list[list]) -> list[list]:
    """Stale audio streams repeat one burst per track sid: bursts that overlap in time are one burst."""
    out: list[list] = []
    for s, e in sorted(raw):
        if out and s <= out[-1][1] + 0.1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def sentences(text: str) -> list[tuple[str, bool]]:
    """(sentence, complete). The last piece of a line with no end punctuation is a cut sentence."""
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]
    return [(p, bool(re.search(r"[.!?][\"')]*$", p))) for p in parts]


def load_transcripts(path: str) -> list[list[dict]]:
    raw = open(path).read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = [raw]
    if isinstance(data, dict):
        data = [e.get("message", "") for e in data.get("events", [])] or [data.get("message", "")]
    out = []
    for m in data or []:
        if isinstance(m, str) and m.lstrip().startswith("{"):
            try:
                m = json.loads(m).get("message", m)
            except ValueError:
                pass
        rows = []
        for ln in str(m).splitlines():
            g = TR_LINE.match(ln)
            if g:  # a floored mark is on average 0.5 s early
                rows.append({"n": int(g[1]), "mark": int(g[2]), "t": int(g[2]) + 0.5, "who": g[3],
                             "human": bool(g[4]), "to": g[5], "text": g[6].strip()})
        if rows:
            out.append(rows)
    return out


class Run:
    def __init__(self, scenario: dict, rig=None, worker=None, transcript=None, pick=None):
        people = scenario["people"]
        self.lines = [dict(l, i=i, name=people[l["who"]]["name"], start=None, end=None)
                      for i, l in enumerate(scenario["lines"])]
        self.agents = scenario.get("agents", ["Friday"])
        self.tail = scenario.get("tail", 25)
        self.timeline: list[tuple[float, str, str]] = []
        self.notes: list[str] = []
        self.warns: list[str] = []
        self.bursts = self.requests = self.agent = self.t0 = self.tracks = None
        self.approx = True  # line times estimated from the transcript
        self.invalid = None
        lo = hi = None
        if worker:
            lo, hi = self.load_worker(worker)
        if rig:
            lo, hi = self.load_rig(rig, lo or os.path.getmtime(rig), lo, hi)
        if transcript:
            self.load_transcript(transcript, lo, hi, pick)
        if self.t0 is None:  # no rig clock: put scenario second 0 where the first known line says it was
            known = [l for l in self.lines if l["start"] is not None]
            at = next((l for l in known if "at" in l), None)
            self.t0 = at["start"] - at["at"] if at else min([t for t, _, _ in self.timeline], default=0.0)
        self.attribute()

    # ---- loading -------------------------------------------------------------------------------------
    def load_worker(self, path):
        self.requests = []
        ts = []
        for ln in open(path, errors="replace"):
            if "voice_limit" in ln or "REFUSING" in ln:  # no voice budget, or the worker never registered
                self.invalid = ln.strip()[:200]
            if not ln.startswith("{"):
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if "_aws" in d:
                t = d["_aws"]["Timestamp"] / 1000
                names = [k for k in d if k.startswith(("live.", "huddle."))]
                dims = {k: v for k, v in d.items() if k != "_aws" and k not in names}
                for n in names:
                    if n == "live.request.created":
                        self.requests.append({"t": t, "dims": dims, "line": None})
                    if n.startswith("live.") and n != "live.stt.prompt_updated":
                        ts.append(t)
                        extra = " ".join(f"{k}={v}" for k, v in dims.items())
                        self.timeline.append((t, "worker", f"{n}={d[n]} {extra}".rstrip()))
            elif d.get("name") == "huddle" and "timestamp" in d:
                t = dt.datetime.fromisoformat(d["timestamp"]).timestamp()
                if "refused" in d.get("message", ""):
                    self.notes.append("worker: " + d["message"][:120])
                self.timeline.append((t, "worker", "log: " + d.get("message", "")[:120]))
        return (min(ts), max(ts)) if ts else (None, None)

    def load_rig(self, path, ref, lo, hi):
        rows = []
        for ln in open(path, errors="replace"):
            g = RIG_LINE.match(ln)
            if g:
                rows.append((float(g[1]), int(g[2]) * 3600 + int(g[3]) * 60 + int(g[4]) + int(g[5]) / 1000, g[6], g[7].strip()))
        if not rows:
            self.notes.append(f"rig stdout {path} has no event lines: treated as absent")
            return lo, hi
        # rig stdout has a time of day and no date: take the date that puts it nearest the other evidence
        day = ref // 86400 * 86400
        base = min((day + d * 86400 for d in (-1, 0, 1)), key=lambda b: abs(b + rows[0][1] - ref))
        self.approx = False
        raw, per, open_, said, prev, last = [], {}, {}, {}, None, 0.0
        subbed: dict[str, float] = {}  # track sid tail -> when the rig subscribed it
        for rel, sod, kind, detail in rows:
            if kind == "WORKER_NEVER_READY":
                self.invalid = "the worker never became ready; no scenario was played"
            if prev is not None and sod < prev - 43200:
                base += 86400  # past midnight UTC
            prev, t = sod, base + sod
            last = t
            if kind == "SAY":
                if self.t0 is None:
                    self.t0 = t - rel
                name, _, text = detail.partition(": ")
                # cycle.sh cuts stdout at 170 columns, so the text may be a prefix
                line = next((l for l in self.lines if l["start"] is None and l["name"] == name
                             and l["say"].startswith(text.rstrip())), None)
                if line:
                    line["start"] = t
                    said[name] = line
                    detail = f"[{line['i']}] {detail}"
            elif kind == "SAID" and detail in said:
                said.pop(detail)["end"] = t
            elif kind == "TRACK_SUB":
                g = re.search(r"sid=(\S+)", detail)
                if g:
                    subbed[g[1][-4:]] = t
            elif kind == "AGENT_START":
                open_.setdefault(detail.split(" ")[0], []).append(t)
            elif kind == "AGENT_END":
                g = re.search(r"spoke (\d+(?:\.\d+)?)s", detail)
                starts = open_.get(detail.split(" ")[0])
                if g and starts:  # the END line prints 0.6 s late (or much later on a stale stream): use the duration
                    s = starts.pop(0)
                    raw.append([s, s + float(g[1])])
                    per.setdefault(detail.split(" ")[0], []).append([s, s + float(g[1])])
            if kind not in ("AGENT_START", "AGENT_END") and t >= (self.t0 or 1e18) - 5:
                self.timeline.append((t, "rig", f"{kind} {detail}"))
        # still speaking when the rig left (it leaves `tail` seconds after the last line): the end is not known
        left = max([last] + [l["end"] + self.tail for l in self.lines if l["end"] is not None])
        raw += [[s, left] for starts in open_.values() for s in starts]
        for k, starts in open_.items():
            per.setdefault(k, []).extend([s, left] for s in starts)
        # An old rig left a gone track's reader running, so the next track's burst was also reported under it,
        # identical to the millisecond. Of such twins the live track is the one subscribed last.
        for k, v in per.items():
            v[:] = [b for b in v if not any(
                k2 != k and any(abs(b[0] - c[0]) < 0.05 and abs(b[1] - c[1]) < 0.05 for c in v2)
                and subbed.get(k2[-4:], 0) > subbed.get(k[-4:], 0) for k2, v2 in per.items())]
        self.bursts = merge(raw)
        by_agent: dict[str, list] = {}
        for k, v in per.items():
            by_agent.setdefault(k.split("#")[0], []).extend(v)
        self.tracks = {k: merge(v) for k, v in by_agent.items()}  # one agent = one track name (stale sids merged)
        for s, e in self.bursts:
            over = self.talking(s)
            self.timeline.append((s, "rig", f"AGENT AUDIO {e - s:.1f}s until {self.rel(e)}" + (f" (started over {over})" if over else "")))
        return min(x for x in (lo, self.t0, last) if x is not None), max(hi or last, last)

    def load_transcript(self, path, lo, hi, pick):
        all_ = load_transcripts(path)
        if not all_:
            self.notes.append(f"transcript {path} has no transcript lines: treated as absent")
            return
        if pick is not None:
            rows = all_[pick]
        elif lo is not None:  # the file may hold several runs: take the one inside this run's time range
            rows = max(all_, key=lambda r: sum(lo - 30 <= x["t"] <= hi + 30 for x in r))
            if not any(lo - 30 <= x["t"] <= hi + 30 for x in rows):
                self.notes.append("no transcript overlaps this run's time range: treated as absent")
                return
        else:
            rows = all_[-1]
        self.agent = [r for r in rows if not r["human"]]
        humans = [r for r in rows if r["human"]]
        # scenario line -> the transcript line(s) the transcriber made of it (it rewrites and splits lines)
        p = 0
        for h in humans:
            hw = words(h["text"])
            best = None
            for j in range(p, min(p + 4, len(self.lines))):
                if self.lines[j]["name"] != h["who"] or not hw:
                    continue
                lw = set(words(self.lines[j]["say"]))
                score = sum(w in lw for w in hw) / len(hw)
                if score >= 0.5 and (best is None or score > best[0]):
                    best = (score, j)
            if best:
                p = best[1]
                h["line"] = p
                if self.approx:
                    line = self.lines[p]
                    line["end"] = h["t"] - 0.5 - MARK_LAG
                    line["start"] = line["end"] - len(words(line["say"])) / WORDS_PER_S
        prev = None
        for a in self.agent:  # when did the agent START this line: the burst it is in, or an estimate
            b = next((b for b in reversed(self.bursts or []) if b[0] - 0.5 <= a["t"] <= b[1] + 3), None)
            a["burst"] = b
            a["start"] = max(b[0], prev if prev is not None and prev >= b[0] else b[0]) if b else a["t"] - len(words(a["text"])) / WORDS_PER_S - 0.5
            prev = a["t"]
        for r in rows:
            tag = f"[{r['line']}] " if "line" in r else ""
            to = f" → {r['to']}" if r["to"] else ""
            self.timeline.append((r["t"], "transcr", f"{'PERSON' if r['human'] else 'AGENT '} {tag}{r['who']}{to}: {r['text']}"))

    def attribute(self):
        """A request belongs to the line whose final transcript most recently preceded it (a request follows the
        STT final, 1-3 s after the line ENDS). One created while a later line is still being said belongs to the
        earlier finished line; only a request before every line's end falls back to the line being said."""
        slack = 1.0 if self.approx else 0.0
        for r in self.requests or []:
            done = [l for l in self.lines if l["end"] is not None and l["end"] - slack <= r["t"] - 0.3]
            mid = [l for l in self.lines if l["start"] is not None and l["start"] <= r["t"] and (l["end"] is None or r["t"] < l["end"])]
            line = max(done, key=lambda l: l["end"]) if done else (mid[-1] if mid else None)
            if line:
                r["line"], r["after"] = line["i"], r["t"] - line["end"] if line["end"] else 0.0

    # ---- helpers -------------------------------------------------------------------------------------
    def rel(self, t) -> str:
        return "?" if t is None else f"{t - self.t0:.1f}s"

    def span(self, a, b) -> str:
        return ("the start" if a < -1e17 else self.rel(a)) + " - " + ("the end" if b > 1e17 else self.rel(b))

    def wall(self, t) -> str:
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    def talking(self, t):
        return ", ".join(l["name"] for l in self.lines if l["start"] is not None and l["end"] is not None and l["start"] <= t <= l["end"])

    def L(self, i) -> str:
        l = self.lines[i]
        say = l["say"] if len(l["say"]) <= 46 else l["say"][:43] + "..."
        return f'line {i} ({l["name"]}: "{say}" {self.rel(l["start"])}-{self.rel(l["end"])})'

    def A(self, a) -> str:
        to = f" → {a['to']}" if a["to"] else ""
        return f'[{a["n"]} @{a["mark"]} = {self.rel(a["t"])}] {a["who"]}{to}: "{a["text"]}"'

    def at(self, v, edge, unbounded=None):
        """(epoch, None) or (None, why not). v: line index; None = no bound, gives `unbounded`."""
        if v is None:
            return unbounded, None
        t = self.lines[v][edge]
        if t is None:
            return None, f"line {v} has no time (" + ("not in the transcript" if self.approx else "never said in rig stdout") + ")"
        return t, None

    def window(self, s):
        if "window" in s:
            if self.approx:
                return None, None, "a window in scenario seconds needs rig stdout"
            return self.t0 + s["window"][0], self.t0 + s["window"][1], None
        ws, why = self.at(s.get("from"), s.get("from_edge", "start"), -1e18)
        we, why2 = self.at(s.get("to"), s.get("to_edge", "start"), 1e18)
        if we is not None:
            we += s.get("to_plus", 0)
        return ws, we, why or why2

    def by(self, s):
        """Agent transcript lines, of the agent named by `who` if given."""
        return [a for a in self.agent if "who" not in s or re.search(s["who"], a["who"], re.I)]

    need_rig = "needs audio timing: no rig stdout"
    need_worker = "no worker log"
    need_tr = "no transcript"

    # ---- assertions: each returns (status, evidence) --------------------------------------------------
    def silent_between(self, **s):
        ws, we, why = self.window(s)
        if why:
            return "SKIP", why
        allow, bad = s.get("allow"), []
        if self.agent is None and self.bursts is None:
            return "SKIP", "no transcript and no rig stdout"
        inside = [a for a in self.agent or [] if ws <= a["start"] < we]
        owed = self._owed(inside, allow, s.get("allow_max", 1))
        covered = []
        for b in self.bursts or []:
            if not ws - 0.2 <= b[0] < we:
                continue
            if self.agent is None and allow:
                return "SKIP", f"audio at {self.rel(b[0])}, but no transcript to tell if it is the owed answer"
            mine = [a for a in self.agent or [] if a["burst"] is b]
            covered += mine
            if not mine and self.agent is not None and b[1] - b[0] < 0.4:  # a blip, not speech
                self.warns.append(f"silent_between: {b[1] - b[0]:.2f}s audio blip at {self.rel(b[0])} with no transcript line (ignored)")
                continue
            over, is_owed = self.talking(b[0]), bool(mine) and all(a in owed for a in mine)
            if is_owed and not over:
                continue  # the owed answer, started at a pause
            what = "the owed answer, but started over " + over if is_owed else ("started over " + over if over else "")
            said = "; ".join(self.A(a) for a in mine) or "no transcript line for it"
            bad.append(f"audio {self.rel(b[0])}-{self.rel(b[1])}" + (f" ({what})" if what else "") + f": {said}")
        bad += [f"transcript {self.A(a)}" for a in inside if a not in owed and a not in covered]
        if bad:
            return "FAIL", " | ".join(bad)
        ok = "; allowed owed answer: " + self.A(owed[0]) if owed else ""
        if self.bursts is None:
            return "SKIP", f"no agent transcript line starts in {self.span(ws, we)}{ok}; audio not checked: no rig stdout"
        return "PASS", f"no agent speech starts in {self.span(ws, we)}{ok}" + ("" if self.agent is not None else " (audio only: no transcript)")

    def _owed(self, inside, allow, allow_max):
        """The owed answer: up to allow_max agent lines matching `allow`, plus the lines that follow each one
        from the same agent (an answer is delivered as several sentence units)."""
        if not allow:
            return []
        owed, starts = [], 0
        for a in inside:
            if hit(allow, a["text"]) and starts < allow_max:
                owed.append(a)
                starts += 1
            elif owed and a["who"] == owed[-1]["who"] and self.agent.index(a) == self.agent.index(owed[-1]) + 1:
                owed.append(a)
        return owed

    def _runs(self, i, within):
        if self.requests is None:
            return None, self.need_worker
        if self.lines[i]["end"] is None:
            return None, self.at(i, "end")[1]
        return [r for r in self.requests if r["line"] == i and r["after"] <= within], None

    def _runs_txt(self, rs):
        return ", ".join(f"created {self.rel(r['t'])} (+{r['after']:.1f}s after the line, by={r['dims'].get('by', '?')})" for r in rs)

    def no_new_run(self, **s):
        out, skipped = [], []
        for i in s.get("lines", [s.get("line")]):
            rs, why = self._runs(i, s.get("within", 10))
            if rs is None:
                skipped.append(why)
            elif rs:
                out.append(f"{self.L(i)}: {self._runs_txt(rs)}")
        if out:
            return "FAIL", " | ".join(out)
        return ("SKIP", "; ".join(sorted(set(skipped)))) if skipped else ("PASS", "no live.request.created after these lines")

    def one_run(self, **s):
        rs, why = self._runs(s["line"], s.get("within", 10))
        if rs is None:
            return "SKIP", why
        return ("PASS" if len(rs) == 1 else "FAIL"), f"{len(rs)} request(s) for {self.L(s['line'])}" + (": " + self._runs_txt(rs) if rs else "")

    def ack_within(self, **s):
        if self.bursts is None:
            return "SKIP", self.need_rig
        end, why = self.at(s["line"], "end")
        if why:
            return "SKIP", why
        b = next((b for b in self.bursts if b[0] >= end - 0.5), None)
        if not b:
            return "FAIL", f"no agent audio after {self.L(s['line'])}"
        return ("PASS" if b[0] - end <= s["seconds"] else "FAIL"), f"first agent audio {b[0] - end:+.1f}s after the line ended (at {self.rel(b[0])}, limit {s['seconds']}s)"

    def answer_contains(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        t0, why = self.at(s["after"], "start")
        t1, why2 = self.at(s.get("before"), "start", 1e18)
        if why or why2:
            return "SKIP", why or why2
        match = [a for a in self.by(s) if a["start"] >= t0 and hit(s["contains"], a["text"]) and not PIECE.search(a["text"])]
        if match and match[0]["start"] < t1:
            return "PASS", self.A(match[0])
        if match:
            return "FAIL", f"said only after line {s['before']} started ({self.rel(t1)}): {self.A(match[0])}"
        said = " | ".join(self.A(a) for a in self.agent if t0 <= a["start"] < t1) or "nothing"
        return "FAIL", f"no agent line after line {s['after']} matches {s['contains']}; the agent said: {said}"

    def said_once(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        keep = s.get("max_overlap_words", 6)
        pieces = [(p, done, a) for a in self.agent if not PIECE.search(a["text"]) for p, done in sentences(a["text"])]
        if "text" in s:  # a short cut fragment is the start of a sentence the agent may finish later
            hits = [(p, a) for p, done, a in pieces if hit(s["text"], p) and (done or len(words(p)) > keep)]
            ev = " | ".join(f'[{a["n"]} @{a["mark"]} = {self.rel(a["t"])}] "{p}"' for p, a in hits)
            return ("PASS" if len(hits) <= 1 else "FAIL"), f"{len(hits)} sentence(s) with /{s['text']}/" + (": " + ev if hits else "")
        bad = []
        for x, (p, done, a) in enumerate(pieces):
            pw = words(p)
            if len(pw) < s.get("min_words", 4) or (not done and len(pw) <= keep):
                continue
            for q, _, a2 in pieces[x + 1:]:
                qw = words(q) if done else words(q)[: len(pw)]  # a cut sentence repeats as the START of a later one
                if difflib.SequenceMatcher(None, pw, qw).ratio() >= 0.9:
                    kind = "sentence said twice" if done else f"{len(pw)} words of a cut sentence said again"
                    bad.append(f'{kind}: [{a["n"]} = {self.rel(a["t"])}] "{p}" and [{a2["n"]} = {self.rel(a2["t"])}] "{q}"')
                    break
        return ("FAIL", " | ".join(bad)) if bad else ("PASS", "no completed sentence is repeated")

    def stops_within(self, **s):
        if self.bursts is None:
            return "SKIP", self.need_rig
        line = self.lines[s["line"]]
        if line["start"] is None or line["end"] is None:
            return "SKIP", self.at(s["line"], "end")[1] or self.at(s["line"], "start")[1]
        p, n = line["start"], s["seconds"]
        active = next((b for b in self.bursts if b[0] < p < b[1]), None)
        if not active:
            return "SKIP", f"the agent was not speaking when {line['name']} started at {self.rel(p)} (nothing to stop)"
        late = [b for b in self.bursts if b[1] > p + n and b[0] < line["end"]]
        if not late:
            return "PASS", f"agent audio ended {active[1] - p:.1f}s after {line['name']} started ({self.rel(p)}); silent until they finished ({self.rel(line['end'])})"
        ev = [f"went on until {self.rel(b[1])} ({b[1] - p:.1f}s after)" if b is active else f"started again at {self.rel(b[0])} while {line['name']} was still speaking" for b in late]
        return "FAIL", f"{line['name']} started at {self.rel(p)}; agent audio " + "; ".join(ev)

    def nothing_after(self, **s):
        end, why = self.at(s["line"], "end")
        if why:
            return "SKIP", why
        if self.agent is None and self.bursts is None:
            return "SKIP", "no transcript and no rig stdout"
        cut = end + s.get("grace", 3.0)
        bad = [f"audio {self.rel(b[0])}-{self.rel(b[1])}" for b in self.bursts or [] if b[1] > cut]
        bad += [f"transcript {self.A(a)}" for a in self.agent or [] if a["t"] > cut + (1.0 if self.approx else 0.5)]
        if bad:
            return "FAIL", f"after {self.L(s['line'])}: " + " | ".join(bad)
        if self.bursts is None:
            return "SKIP", "no agent transcript line after the line; audio not checked: no rig stdout"
        return "PASS", f"no agent audio or transcript line later than {self.rel(cut)}"

    def max_agent_lines(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        ws, we, why = self.window(s)
        if why:
            return "SKIP", why
        inside = []
        for a in (a for a in self.by(s) if ws <= a["start"] < we and not PIECE.search(a["text"])):
            p = inside[-1] if inside else None  # "Got it." / "Give me a moment." is one acknowledgement
            if p and p["who"] == a["who"] and len(words(p["text"])) <= 6 and len(words(a["text"])) <= 6 and a["start"] - p["t"] <= 3.0:
                continue
            inside.append(a)
        ev = f"{len(inside)} agent line(s), limit {s['max']}"
        return ("PASS", ev) if len(inside) <= s["max"] else ("FAIL", ev + ": " + " | ".join(self.A(a) for a in inside))

    def no_text(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        bad = [self.A(a) for a in self.by(s) if re.search(s["pattern"], a["text"], re.I)]
        return ("FAIL", " | ".join(bad)) if bad else ("PASS", f"no agent line matches /{s['pattern']}/")

    # ---- added 10-07 for the wider scenario set -----------------------------------------------------
    def no_agent_overlap(self, **s):
        n, bad, seen = s.get("max", 0.3), [], []
        names = sorted(self.tracks or {})
        for x, a in enumerate(names):
            for b in names[x + 1:]:
                for s1, e1 in self.tracks[a]:
                    for s2, e2 in self.tracks[b]:
                        if min(e1, e2) - max(s1, s2) > n:
                            bad.append(f"audio: {a} and {b} both speaking {self.rel(max(s1, s2))}-{self.rel(min(e1, e2))}")
        if len(names) > 1:
            seen.append(f"{len(names)} agent audio tracks")
        rows = self.agent or []
        if len({a["who"] for a in rows}) > 1:  # the tracks may share a name: the transcript is a second look
            seen.append("transcript lines of " + ", ".join(sorted({a["who"] for a in rows})))
            slack = n + (1.5 if self.approx else 1.0)  # a line's start is an estimate
            for x, a in enumerate(rows):
                for b in rows[x + 1:]:
                    if a["who"] != b["who"] and min(a["t"], b["t"]) - max(a["start"], b["start"]) > slack:
                        bad.append(f"transcript: {self.A(a)} overlaps {self.A(b)}")
        if bad:
            return "FAIL", " | ".join(bad)
        if not seen:
            if self.tracks is None and self.agent is None:
                return "SKIP", "no transcript and no rig stdout"
            return "PASS", "at most one agent spoke in this run"
        return "PASS", "no two agents at the same time (" + "; ".join(seen) + ")"

    def no_talk_over(self, **s):
        if self.bursts is None:
            return "SKIP", self.need_rig
        ws, we, why = self.window(s)
        if why:
            return "SKIP", why
        n, bad, worst = s.get("max", 1.0), [], 0.0
        for l in self.lines:
            if l["i"] in s.get("except", []) or l["start"] is None or l["end"] is None or not ws <= l["start"] < we:
                continue
            over = sum(max(0.0, min(e, l["end"]) - max(b, l["start"])) for b, e in self.bursts)
            worst = max(worst, over)
            if over > n:
                bad.append(f"{over:.1f}s of agent audio over {self.L(l['i'])}")
        return ("FAIL", " | ".join(bad)) if bad else ("PASS", f"most agent audio over one person's line: {worst:.1f}s (limit {n}s)")

    def min_sentences(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        t0, why = self.at(s["after"], "start")
        t1, why2 = self.at(s.get("before"), "start", 1e18)
        if why or why2:
            return "SKIP", why or why2
        got = [p for a in self.by(s) if t0 <= a["start"] < t1 for p, done in sentences(a["text"]) if done]
        return ("PASS" if len(got) >= s["n"] else "FAIL"), f"{len(got)} complete agent sentence(s) after line {s['after']}, need {s['n']}"

    def _cut(self, i):
        """(line, the agent burst that was sounding when the line started) or a SKIP."""
        line = self.lines[i]
        if self.bursts is None:
            return None, None, self.need_rig
        if line["start"] is None or line["end"] is None:
            return None, None, self.at(i, "end")[1] or self.at(i, "start")[1]
        active = next((b for b in self.bursts if b[0] < line["start"] < b[1]), None)
        if not active:
            return None, None, f"the agent was not speaking when {line['name']} started at {self.rel(line['start'])} (the line did not land on agent speech)"
        return line, active, None

    def resumes_after(self, **s):
        line, _, why = self._cut(s["line"])
        if why:
            return "SKIP", why
        nxt = next((b for b in self.bursts if b[0] >= line["end"] - 0.3), None)
        if not nxt:
            return "FAIL", f"no agent audio after {self.L(s['line'])}: the cut answer was never finished"
        late = nxt[0] - line["end"]
        ok = late <= s.get("within", 1e18)
        return ("PASS" if ok else "FAIL"), f"agent audio started again {late:.1f}s after the line ended (at {self.rel(nxt[0])}" + (f", limit {s['within']}s)" if "within" in s else ")")

    def keeps_speaking(self, **s):
        line, _, why = self._cut(s["line"])
        if why:
            return "SKIP", why
        a, b = line["start"], line["end"] + s.get("plus", 2.0)
        got = sum(max(0.0, min(e, b) - max(st, a)) for st, e in self.bursts) / (b - a)
        need = s.get("cover", 0.6)
        return ("PASS" if got >= need else "FAIL"), f"agent audio fills {got:.0%} of {self.rel(a)}-{self.rel(b)} (need {need:.0%})"

    def in_order(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        rows, pos = self.by(s), []
        for t in s["texts"]:
            at = next(((x, m.start()) for x, a in enumerate(rows) for m in [re.search(t, a["text"], re.I)] if m), None)
            if at is None:
                return "FAIL", f"/{t}/ is never said"
            pos.append(at)
        ev = "; ".join(f"/{t}/ first in {self.A(rows[x])}" for t, (x, _) in zip(s["texts"], pos))
        return ("PASS" if pos == sorted(pos) and len(set(pos)) == len(pos) else "FAIL"), ev

    def max_runs(self, **s):
        if self.requests is None:
            return "SKIP", self.need_worker
        ws, we, why = self.window(s)
        if why:
            return "SKIP", why
        rs = [r for r in self.requests if ws <= r["t"] < we]
        return ("PASS" if len(rs) <= s["max"] else "FAIL"), f"{len(rs)} request(s), limit {s['max']}" + (": created " + ", ".join(self.rel(r["t"]) for r in rs) if rs else "")

    def max_words(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        ws, we, why = self.window(s)
        if why:
            return "SKIP", why
        rows = [a for a in self.by(s) if ws <= a["start"] < we and not ("allow" in s and hit(s["allow"], a["text"]))]
        n = sum(len(words(a["text"])) for a in rows)
        return ("PASS" if n <= s["max"] else "FAIL"), f"{n} word(s), limit {s['max']}" + (": " + " | ".join(self.A(a) for a in rows) if rows else "")

    def max_agents(self, **s):
        if self.agent is None:
            return "SKIP", self.need_tr
        names = sorted({a["who"] for a in self.agent})
        return ("PASS" if len(names) <= s["max"] else "FAIL"), f"{len(names)} agent(s) spoke ({', '.join(names) or '-'}), limit {s['max']}"

    KINDS = ("silent_between", "no_new_run", "one_run", "ack_within", "answer_contains", "said_once",
             "stops_within", "nothing_after", "max_agent_lines", "no_text",
             "no_agent_overlap", "no_talk_over", "min_sentences", "resumes_after", "keeps_speaking", "in_order",
             "max_runs", "max_words", "max_agents")

    def evaluate(self, expect: list[dict]) -> list[tuple[str, str, str]]:
        out = []
        for e in expect:
            kind = next((k for k in e if k in self.KINDS), None)
            if not kind:
                out.append(("FAIL", json.dumps(e), "unknown assertion"))
                continue
            status, ev = getattr(self, kind)(**e[kind])
            out.append((status, f"{kind} {json.dumps(e[kind])}" + (f"  # {e['why']}" if e.get("why") else ""), ev))
        return out

    def latencies(self) -> list[str]:
        """Person finished -> first agent audio, for each line that names an agent."""
        out = []
        for l in self.lines:
            if not re.search(r"\b(" + "|".join(map(re.escape, self.agents)) + r")\b", l["say"], re.I):
                continue
            if self.bursts is None or l["end"] is None:
                out.append(f"line {l['i']}: not measured ({self.need_rig if self.bursts is None else 'line never said'})")
                continue
            b = next((b for b in self.bursts if b[0] >= l["end"] - 0.5), None)
            over = f", started over {self.talking(b[0])}" if b and self.talking(b[0]) else ""
            out.append(f"{self.L(l['i'])}: " + (f"first agent audio {b[0] - l['end']:+.2f}s after they finished{over}" if b else "no agent audio after it"))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenario")
    ap.add_argument("--rig")
    ap.add_argument("--worker")
    ap.add_argument("--transcript")
    ap.add_argument("--pick", type=int, help="which transcript in the file (default: the one in this run's time range, else the last)")
    ap.add_argument("--strict", action="store_true", help="exit 2 if an assertion was skipped")
    ap.add_argument("-q", action="store_true", help="no timeline")
    a = ap.parse_args()
    scenario = json.load(open(a.scenario))
    for name in ("rig", "worker", "transcript"):  # a missing or empty file is an absent source, said out loud
        p = getattr(a, name)
        if p and not (os.path.exists(p) and os.path.getsize(p)):
            print(f"NOTE  {name} evidence {p} is missing or empty: treated as absent")
            setattr(a, name, None)
    run = Run(scenario, a.rig, a.worker, a.transcript, a.pick)
    for n in run.notes:
        print("NOTE ", n)
    if run.invalid:
        print("INVALID RUN (no voice budget, or no rig worker; the results below are not about the voice):", run.invalid)
    if run.approx:
        print("NOTE  no rig stdout: line times are estimates from transcript marks (about +-1.5 s); audio assertions are skipped")
    if not a.q:
        print(f"== timeline (seconds on the rig clock; second 0 = {run.wall(run.t0)} UTC) ==")
        for t, src, text in sorted(run.timeline):
            print(f"{t - run.t0:8.2f}  {run.wall(t)}  {src:<7} {text}")
    print("== assertions ==")
    results = run.evaluate(scenario.get("expect", []))
    for status, what, ev in results:
        print(f"{status}  {what}\n      {ev}")
    if not results:
        print('(the scenario has no "expect" block)')
    print("== latency: person finished -> first agent audio ==")
    print("\n".join("  " + x for x in run.latencies()) or "  (no line names an agent)")
    emf = [x for t, src, x in sorted(run.timeline) if src == "worker" and "live.latency.first_audio" in x]
    if emf:
        print("  worker's own numbers (wake -> first audio, ms): " + "; ".join(x.replace("live.latency.first_audio=", "") for x in emf))
    for w in run.warns:
        print("WARN ", w)
    count = {k: sum(r[0] == k for r in results) for k in ("PASS", "FAIL", "SKIP")}
    print(f"== result: {count['PASS']} pass, {count['FAIL']} fail, {count['SKIP']} skip ==")
    return 1 if count["FAIL"] or run.invalid else 2 if a.strict and count["SKIP"] else 0


if __name__ == "__main__":
    sys.exit(main())
