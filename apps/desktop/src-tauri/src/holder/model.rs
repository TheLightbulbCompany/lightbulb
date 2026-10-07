//! The holder's copy of the screen: what a newly attached terminal is shown, and the answers
//! to the terminal queries the provider sends, so a session with no window still starts and
//! two windows never both answer.

use std::time::{Duration, Instant};

/// Queries the provider sends and the fixed answer the holder gives. CPR (`ESC[6n`) is
/// answered from the model's cursor instead.
const PROBES: &[(&[u8], &[u8])] = &[
    (b"\x1b[c", b"\x1b[?62;22c"),
    (b"\x1b[0c", b"\x1b[?62;22c"),
    (b"\x1b[>0q", b"\x1bP>|xterm(lightbulb)\x1b\\"),
    (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:e6e6/e6e6/e9e9\x1b\\"),
    (b"\x1b]10;?\x07", b"\x1b]10;rgb:e6e6/e6e6/e9e9\x07"),
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:1616/1616/1a1a\x1b\\"),
    (b"\x1b]11;?\x07", b"\x1b]11;rgb:1616/1616/1a1a\x07"),
];
const CPR: &[u8] = b"\x1b[6n";
/// Kitty keyboard protocol query. The holder does not claim kitty support, so it is not answered.
const KITTY_QUERY: &[u8] = b"\x1b[?u";

/// Provider output for a client that is a real terminal: without the queries the holder has
/// already answered. The terminal would answer them again, and its answers, read in one burst,
/// would reach the provider as typing.
// ponytail: a query split across two output chunks still reaches the terminal; its lone reply
// is then dropped by `is_probe_reply`. Carry a tail like `Model::feed` if that shows up.
pub fn strip_queries(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(bytes.len());
    let mut rest = bytes;
    'scan: while let Some((&first, tail)) = rest.split_first() {
        if first == 0x1b {
            for query in PROBES.iter().map(|(q, _)| *q).chain([CPR, KITTY_QUERY]) {
                if let Some(after) = rest.strip_prefix(query) {
                    rest = after;
                    continue 'scan;
                }
            }
        }
        out.push(first);
        rest = tail;
    }
    out
}

pub struct Model {
    parser: vt100::Parser,
    /// The last bytes of the previous read: a query split across two reads is still answered.
    tail: Vec<u8>,
    /// The alternate screen as it was when the provider last left it, and when that was.
    left_alternate: Option<(Vec<u8>, Instant)>,
    /// A control sequence the last read ended inside (`ESC[` and parameters, no final byte yet):
    /// one that leaves the alternate screen is found whole however the reads split it.
    partial_csi: Vec<u8>,
    /// When the provider last wrote.
    last_output: Option<Instant>,
    /// When someone last typed, and whether that left a line unsent (no Enter yet).
    typed: Option<(Instant, bool)>,
    /// The screen as it rested before the line now being typed (or just sent) was begun.
    resting: Option<Resting>,
    /// The holder was told to end the provider: no typed line ends this session.
    ending: bool,
    /// The provider is Claude Code in its classic renderer (`run::CLASSIC_RENDERER`): it draws
    /// on the normal screen, so that screen at rest is kept too.
    classic: bool,
}
struct Resting {
    picture: Vec<u8>,
    began: Instant,
    /// When Enter sent that line.
    sent: Option<Instant>,
    /// For a picture of the normal screen: a terminal fed everything since. Its scrollback is
    /// the lines that have left the top of the screen since the picture.
    since: Option<vt100::Parser>,
}
/// No real control sequence is longer; a longer run of parameter bytes is not carried.
const PARTIAL_CSI_MAX: usize = 64;

/// The control sequence `bytes` ends inside, if any: from its last ESC, when only `[` and
/// parameter bytes follow it.
fn partial_csi(bytes: &[u8]) -> &[u8] {
    let Some(start) = bytes.iter().rposition(|b| *b == 0x1b) else { return &[] };
    let rest = &bytes[start..];
    let open = rest.len() == 1 || (rest[1] == b'[' && rest[2..].iter().all(|b| (0x30..=0x3f).contains(b)));
    if open && rest.len() <= PARTIAL_CSI_MAX { rest } else { &[] }
}
const TAIL: usize = 16;
/// A provider that exits this soon after leaving the alternate screen left it to shut down (Claude
/// Code's fullscreen renderer on `/exit`); a longer gap is a full-screen program that closed mid-session.
pub const SHUTDOWN_LEAVE: Duration = Duration::from_secs(5);
/// A screen with no output for this long is at rest: what the person is looking at, not a frame
/// of an answer being drawn.
pub const QUIET: Duration = Duration::from_millis(1500);
/// A line left unsent this long is no longer the line being typed: the next key begins a new one.
/// Also how old a resting picture may be when the line that followed it ends the session.
pub const RESTING_FOR: Duration = Duration::from_secs(30);
/// A provider that leaves the alternate screen this soon after an Enter left because of that line
/// (`/exit`: Claude Code 2.1.292 leaves within 10 ms).
pub const SENT_LEAVE: Duration = Duration::from_secs(3);
/// What `picture`, a `rows` x `cols` screen, shows once its top `gone` lines have scrolled away.
fn scrolled(picture: &[u8], (rows, cols): (u16, u16), gone: u16) -> Vec<u8> {
    let mut screen = vt100::Parser::new(rows, cols, 0);
    screen.process(picture);
    let (row, col) = screen.screen().cursor_position();
    let feeds = "\n".repeat(gone as usize);
    screen.process(format!("\x1b[{rows};1H{feeds}\x1b[{};{}H", row.saturating_sub(gone) + 1, col + 1).as_bytes());
    let mut out = screen.screen().contents_formatted();
    out.extend_from_slice(MOUSE_OFF);
    out
}

/// Mouse reporting off: a kept picture is read-only, and xterm selects no text while it is on.
const MOUSE_OFF: &[u8] = b"\x1b[?9;1000;1002;1003l";

/// Where in the new bytes (after the first `skip` bytes of `hay`) each sequence that leaves the
/// alternate screen starts: `ESC[?…l` naming 47 or 1049, the two the model acts on. One that began
/// in the bytes before starts at 0.
pub(crate) fn alternate_leaves(hay: &[u8], skip: usize) -> Vec<usize> {
    let mut cuts = Vec::new();
    for start in 0..hay.len() {
        let Some(rest) = hay[start..].strip_prefix(b"\x1b[?") else { continue };
        let params = rest.iter().take_while(|b| b.is_ascii_digit() || **b == b';').count();
        let end = start + 3 + params + 1;
        if rest.get(params) == Some(&b'l') && end > skip && rest[..params].split(|b| *b == b';').any(|p| p == b"47" || p == b"1049") {
            cuts.push(start.saturating_sub(skip));
        }
    }
    cuts
}

/// Where in the new bytes (after the first `skip` bytes of `hay`) each `needle` ends.
fn ends<'a>(hay: &'a [u8], needle: &'a [u8], skip: usize) -> impl Iterator<Item = usize> + 'a {
    hay.windows(needle.len()).enumerate().filter(move |(i, w)| *w == needle && i + needle.len() > skip).map(move |(i, _)| i + needle.len() - skip)
}

/// How many times `needle` ends inside the new bytes (after the first `skip` bytes of `hay`).
fn completed(hay: &[u8], needle: &[u8], skip: usize) -> usize {
    ends(hay, needle, skip).count()
}

/// Lines the model keeps above its screen: what `Model::history` can give a new viewer.
const SCROLLBACK: usize = 5000;
/// Erase the saved lines: a terminal drops everything above its screen. vt100 does not act on it.
const ERASE_SAVED: &[u8] = b"\x1b[3J";

impl Model {
    pub fn new(rows: u16, cols: u16) -> Self {
        Self { parser: vt100::Parser::new(rows, cols, SCROLLBACK), tail: Vec::new(), left_alternate: None, partial_csi: Vec::new(),
            last_output: None, typed: None, resting: None, ending: false, classic: false }
    }

    /// The provider is Claude Code in its classic renderer.
    pub fn classic_claude_code(&mut self) {
        self.classic = true;
    }

    /// Feed provider output. Returns what to write back to the pty: the probe answers.
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<u8> {
        self.feed_at(bytes, Instant::now())
    }

    /// The screen as a picture for a normal buffer: read-only, so no mouse reporting.
    fn picture(&self) -> Vec<u8> {
        let mut picture = self.parser.screen().contents_formatted();
        picture.extend_from_slice(MOUSE_OFF);
        picture
    }

    /// The screen as it is now, as the picture to keep if the session ends from here.
    fn rest(&self, now: Instant) -> Resting {
        let screen = self.parser.screen();
        Resting { picture: self.picture(), began: now, sent: None, since: (!screen.alternate_screen()).then(|| {
            // Lines leave the top when the cursor passes the bottom, so the cursor is all it needs to start from.
            let ((rows, cols), (row, col)) = (screen.size(), screen.cursor_position());
            let mut term = vt100::Parser::new(rows, cols, rows as usize);
            term.process(format!("\x1b[{};{}H", row + 1, col + 1).as_bytes());
            term
        }) }
    }

    /// Someone (the owner or a teammate) is about to type `bytes`. The first key of a new line,
    /// typed at a screen at rest, keeps that screen: the picture the person was reading before
    /// the command that may end the session (`/exit` and its menu) was drawn over it.
    pub fn typed(&mut self, bytes: &[u8], now: Instant) {
        // Mouse and focus reports are not typing.
        if self.ending || [&b"\x1b[<"[..], b"\x1b[M", b"\x1b[I", b"\x1b[O"].iter().any(|report| bytes.starts_with(report)) {
            return;
        }
        let since = |at: Instant| now.saturating_duration_since(at);
        let unsent = self.typed.is_some_and(|(at, unsent)| unsent && since(at) < RESTING_FOR);
        if !unsent {
            let at_rest = (self.parser.screen().alternate_screen() || self.classic) && self.last_output.is_none_or(|at| since(at) >= QUIET);
            // (Typed over an answer still being drawn: no picture, and the one before an earlier line is not this line's.)
            self.resting = at_rest.then(|| self.rest(now));
        }
        let enter = bytes.contains(&b'\r');
        if let Some(resting) = self.resting.as_mut().filter(|_| enter) {
            resting.sent = Some(now);
        }
        self.typed = Some((now, !enter));
    }

    /// The holder is ending the provider itself (END). The leave that follows answers that, not
    /// the last typed line, so the picture kept is the screen as it is then: a reply drawn right
    /// after that line's Enter stays in it.
    ///
    /// The classic renderer has no leave to mark that moment: it prints its resume lines on the
    /// same screen (`CLAUDE_CLASSIC_SIGNAL_READS`). So its picture is taken here, and
    /// `ended_picture` takes off its top the lines the way out pushes above the screen.
    pub fn ending(&mut self) {
        self.ending = true;
        self.resting = (self.classic && !self.parser.screen().alternate_screen()).then(|| self.rest(Instant::now()));
    }

    /// `ESC[3J` on the normal screen: the lines above it are gone from every terminal (Claude
    /// Code's classic renderer clears the terminal and prints the conversation again). The model
    /// starts over from its own screen, which is all a terminal has left.
    // ponytail: the scroll region and the saved cursor do not survive this; classic Claude Code
    // sets neither while it runs. Keep them if a provider that does ever needs this.
    fn erase_saved(&mut self) {
        if self.parser.screen().alternate_screen() {
            return;
        }
        let (rows, cols) = self.size();
        let mut parser = vt100::Parser::new(rows, cols, SCROLLBACK);
        parser.process(&self.snapshot());
        self.parser = parser;
    }

    /// The lines above the screen, oldest first, as bytes for an empty terminal: they end up in
    /// its scrollback, above a blank screen for the snapshot that follows. Whole lines, the newest
    /// that fit `max` bytes; empty when nothing is above the screen. Unlike a tail of the output
    /// log, it starts clean anywhere: the classic renderer redraws with cursor moves relative to
    /// where it was, so its log read from the middle draws over the wrong rows.
    pub fn history(&mut self, max: usize) -> Vec<u8> {
        let (rows, cols) = self.size();
        let screen = self.parser.screen_mut();
        screen.set_scrollback(usize::MAX);
        let above = screen.scrollback();
        // Line feeds after the last line: they push it, too, off the empty terminal's screen.
        let feeds = usize::from(rows).saturating_sub(1);
        let (mut lines, mut size) = (Vec::new(), feeds);
        for back in 1..=above {
            screen.set_scrollback(back);
            let Some(mut line) = screen.rows_formatted(0, cols).next() else { break };
            line.extend_from_slice(b"\x1b[m\r\n");
            size += line.len();
            if size > max {
                break;
            }
            lines.push(line);
        }
        screen.set_scrollback(0);
        if lines.is_empty() {
            return Vec::new();
        }
        let mut out: Vec<u8> = lines.into_iter().rev().flatten().collect();
        out.resize(out.len() + feeds, b'\n');
        out
    }

    pub fn feed_at(&mut self, bytes: &[u8], now: Instant) -> Vec<u8> {
        self.last_output = Some(now);
        // A picture too old to be the one before the line that ends the session (every rule below
        // and `ended_picture` would refuse it) is dropped, and its terminal with it.
        let old = |at: Instant, limit: Duration| now.saturating_duration_since(at) >= limit;
        // (Not the picture taken at END: nothing typed can follow it, and it is the one to keep however long the way out takes.)
        if !self.ending && self.resting.as_ref().is_some_and(|r| old(r.began, RESTING_FOR) || r.sent.is_some_and(|at| old(at, SHUTDOWN_LEAVE))) {
            self.resting = None;
        }
        if let Some(since) = self.resting.as_mut().and_then(|r| r.since.as_mut()) {
            since.process(bytes);
        }
        let skip = self.tail.len();
        let mut seen = std::mem::take(&mut self.tail);
        seen.extend_from_slice(bytes);
        let mut answers = Vec::new();
        for (query, answer) in PROBES {
            for _ in 0..completed(&seen, query, skip) {
                answers.extend_from_slice(answer);
            }
        }
        // Stop just before each leave of the alternate screen, to keep the picture it ends, and
        // (classic Claude Code) just after each erase of the saved lines.
        let mut done = 0;
        let mut csi = std::mem::take(&mut self.partial_csi);
        let carried = csi.len();
        csi.extend_from_slice(bytes);
        self.partial_csi = partial_csi(&csi).to_vec();
        let mut cuts: Vec<(usize, bool)> = alternate_leaves(&csi, carried).into_iter().map(|cut| (cut, true)).collect();
        if self.classic {
            cuts.extend(ends(&seen, ERASE_SAVED, skip).map(|cut| (cut, false)));
            cuts.sort_unstable();
        }
        for (cut, leave) in cuts {
            self.parser.process(&bytes[done..cut]);
            done = cut;
            if !leave {
                self.erase_saved();
            } else if self.parser.screen().alternate_screen() {
                // Left right after a line was sent: that line ended the session, and the picture
                // from before it was typed is the one to keep.
                let since = |at: Instant| now.saturating_duration_since(at);
                let rested = self.resting.take().filter(|r| r.since.is_none() && since(r.began) < RESTING_FOR && r.sent.is_some_and(|at| since(at) < SENT_LEAVE));
                self.left_alternate = Some((rested.map_or_else(|| self.picture(), |r| r.picture), now));
            }
        }
        self.parser.process(&bytes[done..]);
        for _ in 0..completed(&seen, CPR, skip) {
            let (row, col) = self.parser.screen().cursor_position();
            answers.extend_from_slice(format!("\x1b[{};{}R", row + 1, col + 1).as_bytes());
        }
        self.tail = seen[seen.len().saturating_sub(TAIL)..].to_vec();
        answers
    }

    pub fn resize(&mut self, rows: u16, cols: u16) {
        self.parser.screen_mut().set_size(rows, cols);
        // A picture of the normal screen is its lines at that size: it does not survive a resize.
        if self.resting.as_ref().is_some_and(|r| r.since.is_some()) {
            self.resting = None;
        }
    }

    pub fn size(&self) -> (u16, u16) {
        self.parser.screen().size()
    }

    /// The screen and its input modes, ready to write into a fresh terminal.
    pub fn snapshot(&self) -> Vec<u8> {
        let screen = self.parser.screen();
        let mut out = Vec::new();
        if screen.alternate_screen() {
            out.extend_from_slice(b"\x1b[?1049h");
        }
        out.extend(screen.state_formatted());
        out
    }

    /// The last picture of a provider that exited at `exited` on its way out of the alternate
    /// screen: what the person saw while it ran, not the lines it printed after. Drawn as normal-screen
    /// content with no input modes (no `ESC[?1049h`): an alternate buffer has no scrollback, and the
    /// picture is only read. A provider that died on the alternate screen (killed) left that
    /// screen as its last picture.
    ///
    /// Claude Code's classic renderer never leaves the normal screen: on `/exit` it leaves the
    /// slash-command menu drawn and prints its resume lines under it (`CLAUDE_CLASSIC_EXIT_READS`).
    /// When the line sent last ended the session (the exit came within `SHUTDOWN_LEAVE` of its
    /// Enter; 2.1.293 took 0.39 s), the picture is the screen at rest before that line, without
    /// the lines that have scrolled off its top since: a viewer's terminal has those in its
    /// scrollback, so its history and this picture are the session as it was at rest. The same
    /// for the picture taken when the holder ended it (`ending`), however long the way out took.
    ///
    /// None when the final screen is the last picture.
    pub fn ended_picture(&self, exited: Instant) -> Option<Vec<u8>> {
        let screen = self.parser.screen();
        if screen.alternate_screen() {
            return Some(self.picture());
        }
        let within = |at: Instant, limit: Duration| exited.saturating_duration_since(at) < limit;
        if let Some((picture, _)) = self.left_alternate.as_ref().filter(|(_, left)| within(*left, SHUTDOWN_LEAVE)) {
            return Some(picture.clone());
        }
        let rested = self.resting.as_ref().filter(|r| self.ending || (within(r.began, RESTING_FOR) && r.sent.is_some_and(|at| within(at, SHUTDOWN_LEAVE))))?;
        let mut since = rested.since.as_ref()?.screen().clone();
        since.set_scrollback(usize::MAX);
        let (gone, (rows, cols)) = (since.scrollback() as u16, since.size());
        // (A whole screen gone: its lines are a viewer's history already, and the final screen follows them.)
        (gone < rows).then(|| scrolled(&rested.picture, (rows, cols), gone))
    }
}

/// A client's reply to a terminal query (DA, XTVERSION, OSC 10/11 colour, CPR, kitty flags).
/// Dropped: the holder answers queries itself.
pub fn is_probe_reply(input: &[u8]) -> bool {
    let Ok(s) = std::str::from_utf8(input) else { return false };
    let csi_body = |s: &str, end: char| {
        s.strip_prefix("\x1b[").and_then(|r| r.strip_suffix(end)).map(|b| b.to_owned())
    };
    let digits = |b: &str| !b.is_empty() && b.chars().all(|c| c.is_ascii_digit() || c == ';');
    s.starts_with("\x1bP>|")
        || s.starts_with("\x1b]10;rgb")
        || s.starts_with("\x1b]11;rgb")
        || csi_body(s, 'c').is_some_and(|b| b.starts_with('?') && digits(&b[1..]))
        || csi_body(s, 'u').is_some_and(|b| b.starts_with('?') && digits(&b[1..]))
        || csi_body(s, 'R').is_some_and(|b| digits(&b) && b.contains(';'))
}

/// Claude Code 2.1.292, `tui: fullscreen`, in a 120x40 pty (captured 2026-10-07): its reads
/// from Enter on `/exit` to the end, as they arrived. It exited 0.47 s after the first.
#[cfg(test)]
pub(crate) const CLAUDE_EXIT_READS: &[&[u8]] = &[
    b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l",
    b"\x1b[?25h",
    b"\x1b[?1004l\x1b[?1049l\x1b[>4m\x1b[>4m\x1b[?2031l\x1b[?2004l\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b7\x1b[r\x1b8",
    b"\x1b[?25h",
    b"\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[>4m\x1b[?1004l\x1b[?2031l\x1b[?2004l\x1b[?25h\x1b7\x1b[r\x1b8\x1b]0;\x07",
    b"\x1b[2m\x1b[22m\r\n\x1b[2mResume this session with:\x1b[22m\r\n\x1b[2mclaude --resume 5591f323-31ca-4057-846d-1a38487a091b\x1b[22m\r\n\x1b[2m\x1b[22m",
    b"\x1b[?25h",
];

/// Claude Code 2.1.293 in its classic renderer (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`), a
/// 120x40 pty, captured 2026-10-07: its reads from Enter on `/exit` to the end, as they arrived
/// (the first 8 ms after the Enter). It exited 0.39 s after that Enter, code 0. It never leaves the
/// normal screen: the cursor goes down past the slash-command menu, which stays drawn, and the
/// resume lines are printed under it. The whole session is `tests/fixtures/claude_classic_exit.json`.
#[cfg(test)]
pub(crate) const CLAUDE_CLASSIC_EXIT_READS: &[&[u8]] = &[
    b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l",
    b"\x1b[7D\x1b[22B",
    b"\x1b[?1004l\x1b[>4m\x1b[?2031l\x1b[?2004l\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b7\x1b[r\x1b8\x1b]0;\x07",
    b"\x1b[2m\x1b[22m\r\n\x1b[2mResume this session with:\x1b[22m\r\n\x1b[2mclaude --resume 49ec9320-3f6c-43dd-8f52-19215d19341d\x1b[22m\r\n\x1b[2m\x1b[22m",
];
/// The same build and renderer when its process group gets SIGHUP (what END sends), at an empty
/// prompt: first read 1 ms after the signal, exit 129 after 0.44 s.
#[cfg(test)]
pub(crate) const CLAUDE_CLASSIC_SIGNAL_READS: &[&[u8]] = &[
    b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l",
    b"\x1b[2D\x1b[3B",
    b"\x1b[?1004l\x1b[>4m\x1b[?2031l\x1b[?2004l\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b7\x1b[r\x1b8\x1b]0;\x07",
    b"\x1b[2m\x1b[22m\r\n\x1b[2mResume this session with:\x1b[22m\r\n\x1b[2mclaude --resume 1dd56840-2d92-4d3d-8eab-7702e7bb0d5e\x1b[22m\r\n\x1b[2m\x1b[22m",
];

#[cfg(test)]
mod tests {
    use super::*;

    /// A viewer's terminal: its scrollback and its screen, a line each, without the blank end.
    fn lines(term: &mut vt100::Parser) -> Vec<String> {
        let cols = term.screen().size().1;
        term.screen_mut().set_scrollback(usize::MAX);
        let above = term.screen().scrollback();
        let mut lines = Vec::new();
        for back in (1..=above).rev() {
            term.screen_mut().set_scrollback(back);
            lines.extend(term.screen().rows(0, cols).next());
        }
        term.screen_mut().set_scrollback(0);
        lines.extend(term.screen().rows(0, cols));
        let mut lines: Vec<String> = lines.iter().map(|l| l.trim_end().to_owned()).collect();
        while lines.last().is_some_and(|l| l.is_empty()) {
            lines.pop();
        }
        lines
    }

    /// How the web viewer draws a snapshot (`SessionTerminalView` CLEAR): the visible screen only.
    const WEB_CLEAR: &[u8] = b"\x1b[?1049l\x1b[r\x1b[2J\x1b[H";

    /// Owner ruling 2026-10-07: held Claude Code sessions run in the classic renderer. Observed in
    /// prod that day: after `/exit` the kept picture ended with the open slash menu and the resume
    /// lines. This replays a real session (every read and key with its time, `[ms, kind, bytes as
    /// Latin-1]`; Claude Code 2.1.293, 120x40, two prompts, then `/exit` a key every 0.5 s and Enter).
    #[test]
    fn holder_model_keeps_the_screen_real_classic_claude_code_rested_on_before_exit() {
        let events: Vec<(u64, String, String)> = serde_json::from_str(include_str!("../../tests/fixtures/claude_classic_exit.json")).unwrap();
        let capture: Vec<(Duration, &str, Vec<u8>)> =
            events.iter().map(|(ms, kind, data)| (Duration::from_millis(*ms), kind.as_str(), data.chars().map(|c| c as u8).collect())).collect();
        let output: Vec<u8> = capture.iter().filter(|e| e.1 == "o").flat_map(|e| e.2.clone()).collect();
        assert!(alternate_leaves(&output, 0).is_empty() && !output.windows(8).any(|w| w == b"\x1b[?1049h"), "the classic renderer stays on the normal screen");
        let enter = capture.iter().rposition(|e| e.1 == "i" && e.2 == b"\r").unwrap();
        let after: Vec<&[u8]> = capture[enter..].iter().filter(|e| e.1 == "o").map(|e| &e.2[..]).collect();
        assert_eq!(after, CLAUDE_CLASSIC_EXIT_READS);
        // The first key of the line that ended it.
        let slash = capture.iter().rposition(|e| e.1 == "i" && e.2 == b"/").unwrap();
        let t0 = Instant::now();
        let run = |classic: bool| {
            let mut model = Model::new(40, 120);
            if classic {
                model.classic_claude_code();
            }
            // A teammate's terminal that saw all of it.
            let mut viewer = vt100::Parser::new(40, 120, 1000);
            let (mut rested, mut exited) = (vec![], t0);
            for (i, (at, kind, data)) in capture.iter().enumerate() {
                if i == slash {
                    rested = lines(&mut viewer);
                }
                match *kind {
                    "o" => {
                        model.feed_at(data, t0 + *at);
                        viewer.process(data);
                    }
                    "i" => model.typed(data, t0 + *at),
                    _ => exited = t0 + *at,
                }
            }
            (model, viewer, rested, exited)
        };
        let (model, mut viewer, rested, exited) = run(true);
        let said = |lines: &[String], text: &str| lines.iter().any(|l| l.contains(text));
        assert!(said(&rested, "Reply with the single word BRAVO.") && !said(&rested, "❯ /exit") && !said(&rested, "Exit the CLI"));
        let last = lines(&mut viewer);
        assert!(said(&last, "❯ /exit") && said(&last, "Exit the CLI") && said(&last, "Resume this session with:") && last.len() > rested.len(), "the final screen has the menu and the resume lines");
        let picture = model.ended_picture(exited).expect("the screen at rest before /exit");
        assert!(!picture.windows(8).any(|w| w == b"\x1b[?1049h"));
        viewer.process(WEB_CLEAR);
        viewer.process(&picture);
        assert_eq!(lines(&mut viewer), rested, "history and picture are the session as it rested: nothing twice, nothing missing, no menu");
        // A viewer who opens it after the end gets the same picture on an empty terminal.
        let mut late = vt100::Parser::new(40, 120, 0);
        late.process(WEB_CLEAR);
        late.process(&picture);
        let shown = lines(&mut late);
        assert!(said(&shown, "Reply with the single word BRAVO.") && !said(&shown, "❯ /exit") && !said(&shown, "Exit the CLI") && !said(&shown, "Resume this session"));
        // Live, the terminal still shows what the provider drew last.
        let mut live = vt100::Parser::new(40, 120, 0);
        live.process(&model.snapshot());
        assert!(said(&lines(&mut live), "Exit the CLI"));
        // The exit came long after that Enter: something else ended it, and the final screen stays.
        assert_eq!(model.ended_picture(exited + SHUTDOWN_LEAVE), None);
        // Any other program on the normal screen (Codex with `--no-alt-screen`, a shell): as before.
        let (other, _, _, exited) = run(false);
        assert_eq!(other.ended_picture(exited), None);
    }

    /// Prod 2026-10-07 (desktop 0.4.666): after End session the kept picture of a classic session
    /// ended with "Resume this session with: / claude --resume <id>". The real session of the test
    /// above up to its `/exit`, then End instead: the real reads of a hang-up at an empty prompt.
    #[test]
    fn holder_model_keeps_the_screen_real_classic_claude_code_showed_when_it_was_ended() {
        let events: Vec<(u64, String, String)> = serde_json::from_str(include_str!("../../tests/fixtures/claude_classic_exit.json")).unwrap();
        let slash = events.iter().rposition(|e| e.1 == "i" && e.2 == "/").unwrap();
        let t0 = Instant::now();
        let mut model = Model::new(40, 120);
        model.classic_claude_code();
        let mut viewer = vt100::Parser::new(40, 120, 1000);
        for (ms, kind, data) in &events[..slash] {
            let (at, bytes) = (t0 + Duration::from_millis(*ms), data.chars().map(|c| c as u8).collect::<Vec<u8>>());
            if kind == "o" {
                model.feed_at(&bytes, at);
                viewer.process(&bytes);
            } else {
                model.typed(&bytes, at);
            }
        }
        let rested = lines(&mut viewer);
        let said = |lines: &[String], text: &str| lines.iter().any(|l| l.contains(text));
        assert!(said(&rested, "Reply with the single word BRAVO.") && !said(&rested, "Resume this session"));
        let end = t0 + Duration::from_millis(events[slash].0);
        model.ending();
        for read in CLAUDE_CLASSIC_SIGNAL_READS {
            model.feed_at(read, end);
            viewer.process(read);
        }
        assert!(said(&lines(&mut viewer), "Resume this session with:"), "the final screen has the resume lines");
        let picture = model.ended_picture(end + Duration::from_millis(440)).expect("the screen End found");
        // Someone who watched it end: their terminal's own lines above, the picture in place of the screen.
        viewer.process(WEB_CLEAR);
        viewer.process(&picture);
        assert_eq!(lines(&mut viewer), rested, "nothing twice, nothing missing, no resume lines");
        // Someone who opens it after the end: the holder's lines above the screen, then the picture.
        let mut late = vt100::Parser::new(40, 120, 1000);
        late.process(&model.history(1 << 20));
        late.process(WEB_CLEAR);
        late.process(&picture);
        assert_eq!(lines(&mut late), rested, "the same session as the one who watched");
    }

    #[test]
    fn holder_model_history_is_the_lines_above_the_screen_whole_and_the_newest_that_fit() {
        let mut model = Model::new(5, 20);
        model.classic_claude_code();
        assert!(model.history(1 << 20).is_empty(), "nothing is above the screen yet");
        for i in 1..=12 {
            model.feed(format!("\x1b[1mline {i}\x1b[m\r\n").as_bytes());
        }
        model.feed(b"> prompt");
        // A new viewer's empty terminal: the history, then the snapshot as the web view draws it.
        let viewer = |history: &[u8], model: &Model| {
            let mut term = vt100::Parser::new(5, 20, 1000);
            term.process(history);
            term.process(WEB_CLEAR);
            term.process(&model.snapshot());
            lines(&mut term)
        };
        let session: Vec<String> = (1..=12).map(|i| format!("line {i}")).chain(["> prompt".to_owned()]).collect();
        let (screen, all) = (model.snapshot(), model.history(1 << 20));
        assert_eq!(model.snapshot(), screen, "asking moves nothing on the holder's own screen");
        assert_eq!(viewer(&all, &model), session, "eight lines above, five on the screen");
        // One byte short of everything: the oldest line goes, whole.
        let most = model.history(all.len() - 1);
        assert_eq!(viewer(&most, &model), session[1..]);
        assert!(most.len() < all.len() && all.ends_with(&most));
        // No room for one line: nothing, never part of a line.
        assert!(model.history(8).is_empty());
        assert_eq!(viewer(&[], &model), session[8..]);
    }

    /// Claude Code's classic renderer clears the terminal, saved lines included, and prints the
    /// conversation again (a resize). A viewer's terminal drops its scrollback then; so does the model.
    #[test]
    fn holder_model_drops_the_lines_above_when_classic_claude_code_erases_them() {
        let print = |model: &mut Model| {
            for i in 1..=12 {
                model.feed(format!("old {i}\r\n").as_bytes());
            }
            // Split across two reads.
            model.feed(b"\x1b[2J\x1b[3");
            model.feed(b"J\x1b[Hnew 1\r\nnew 2\r\nnew 3\r\nnew 4\r\nnew 5\r\nnew 6\r\n> ");
        };
        let viewer = |model: &mut Model| {
            let mut term = vt100::Parser::new(5, 20, 1000);
            term.process(&model.history(1 << 20));
            term.process(WEB_CLEAR);
            term.process(&model.snapshot());
            lines(&mut term)
        };
        let mut model = Model::new(5, 20);
        model.classic_claude_code();
        print(&mut model);
        assert_eq!(viewer(&mut model), ["new 1", "new 2", "new 3", "new 4", "new 5", "new 6", ">"]);
        // Any other program: as before.
        let mut other = Model::new(5, 20);
        print(&mut other);
        assert_eq!(viewer(&mut other)[0], "old 1");
    }

    #[test]
    fn holder_model_keeps_a_classic_rest_only_when_the_line_sent_last_ended_the_session() {
        let secs = |s: f32| Duration::from_secs_f32(s);
        let t0 = Instant::now();
        let drawn = || {
            let mut model = Model::new(10, 40);
            model.classic_claude_code();
            model.feed_at(b"> question\r\nthe last answer\r\n\r\n> ", t0);
            model
        };
        let shown = |picture: Option<Vec<u8>>| {
            picture.map(|picture| {
                let mut term = vt100::Parser::new(10, 40, 0);
                term.process(&picture);
                lines(&mut term)
            })
        };
        let rest = ["> question", "the last answer", "", ">"].map(String::from).to_vec();
        const MENU: &[u8] = b"/exit\r\n  /exit     Exit the CLI\r\n  /context  Show context";
        const RESUME: &[u8] = b"\r\n\r\nResume this session with:\r\nclaude --resume 1";

        // Everything still fits: the picture is the whole screen at rest.
        let mut model = drawn();
        model.typed(b"/exit", t0 + secs(10.0));
        model.feed_at(MENU, t0 + secs(10.0));
        model.typed(b"\r", t0 + secs(11.0));
        model.feed_at(RESUME, t0 + secs(11.01));
        assert_eq!(shown(model.ended_picture(t0 + secs(11.4))), Some(rest.clone()));

        // Two lines left the top on the way out: a viewer has them above, so the picture starts after them.
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.feed_at(MENU, t0 + secs(10.0));
        model.feed_at(b"\r\n  /more\r\n  /more\r\n  /more", t0 + secs(10.0));
        model.feed_at(RESUME, t0 + secs(10.01));
        assert_eq!(shown(model.ended_picture(t0 + secs(10.4))), Some(rest[2..].to_vec()));

        // The whole screen left the top: the final screen is the picture.
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.feed_at(&b"\r\nline".repeat(16), t0 + secs(10.0));
        assert_eq!(model.ended_picture(t0 + secs(10.4)), None);

        // End session at an empty prompt (real reads; prod 2026-10-07: the kept picture ended with the
        // resume lines). The picture is the screen as End found it, the quick reply included, without
        // the three lines the way out pushed above it (40 columns: the resume command wraps); however
        // long the way out took (a kill after 5 s).
        let mut model = drawn();
        model.typed(b"another question\r", t0 + secs(10.0));
        model.feed_at(b"\r\na quick reply\r\n> ", t0 + secs(10.5));
        model.ending();
        model.typed(b"typed after End\r", t0 + secs(10.9));
        for read in CLAUDE_CLASSIC_SIGNAL_READS {
            model.feed_at(read, t0 + secs(11.0));
        }
        let ended = [">", "a quick reply", ">"].map(String::from).to_vec();
        assert_eq!(shown(model.ended_picture(t0 + secs(11.44))), Some(ended.clone()));
        assert_eq!(shown(model.ended_picture(t0 + secs(11.0) + SHUTDOWN_LEAVE + RESTING_FOR)), Some(ended));
        let mut live = vt100::Parser::new(10, 40, 0);
        live.process(&model.snapshot());
        assert!(lines(&mut live).iter().any(|l| l == "Resume this session with:"), "live, the terminal shows what the provider drew last");

        // Typed while an answer was still being drawn, a line never sent, and a resize in between: the final screen.
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(1.0));
        model.feed_at(RESUME, t0 + secs(1.01));
        assert_eq!(model.ended_picture(t0 + secs(1.4)), None);
        let mut model = drawn();
        model.typed(b"/exit", t0 + secs(10.0));
        model.feed_at(MENU, t0 + secs(10.0));
        assert_eq!(model.ended_picture(t0 + secs(10.4)), None);
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.resize(12, 40);
        model.feed_at(RESUME, t0 + secs(10.01));
        assert_eq!(model.ended_picture(t0 + secs(10.4)), None);

        // A question answered for longer than a shutdown takes: its picture is dropped, not carried.
        let mut model = drawn();
        model.typed(b"a question\r", t0 + secs(10.0));
        model.feed_at(b"a long answer", t0 + secs(10.0) + SHUTDOWN_LEAVE);
        assert!(model.resting.is_none());
    }

    #[test]
    fn holder_model_strips_answered_queries_for_a_real_terminal() {
        assert_eq!(strip_queries(b"a\x1b[cb\x1b]11;?\x07c\x1b[6nd\x1b[?ue\x1b[>0qf"), b"abcdef");
        let drawing = b"\x1b[31mred\x1b[0m\x1b[?2004h\x1b[?1049h\x1b[?1u";
        assert_eq!(strip_queries(drawing), drawing.to_vec(), "everything else passes untouched");
    }

    #[test]
    fn holder_model_answers_the_queries_claude_sends_at_start() {
        let mut model = Model::new(40, 120);
        assert_eq!(model.feed(b"\x1b[?u\x1b[c"), b"\x1b[?62;22c");
        assert_eq!(model.feed(b"\x1b]11;?\x07"), b"\x1b]11;rgb:1616/1616/1a1a\x07");
        assert_eq!(model.feed(b"plain output"), b"");
        assert_eq!(model.feed(b"\x1b[3;5H\x1b[6n"), b"\x1b[3;5R");
    }

    #[test]
    fn holder_model_answers_a_query_split_across_two_reads_exactly_once() {
        let mut model = Model::new(40, 120);
        assert_eq!(model.feed(b"text \x1b]11"), b"");
        assert_eq!(model.feed(b";?\x07 more"), b"\x1b]11;rgb:1616/1616/1a1a\x07");
        assert_eq!(model.feed(b"later"), b"", "an answered query is not answered again");
    }

    #[test]
    fn holder_model_snapshot_carries_modes_and_the_alternate_screen() {
        let mut model = Model::new(10, 40);
        model.feed(b"\x1b[?2004h");
        assert!(model.snapshot().windows(8).any(|w| w == b"\x1b[?2004h"));
        model.feed(b"\x1b[?1049hX");
        assert!(model.snapshot().starts_with(b"\x1b[?1049h"));
    }

    #[test]
    fn holder_model_keeps_the_alternate_picture_a_provider_leaves() {
        let has = |hay: &[u8], needle: &[u8]| hay.windows(needle.len()).any(|w| w == needle);
        let now = Instant::now();
        // Split across two reads, and one of several modes in one sequence.
        for leave in [&[&b"\x1b[?1049l"[..]][..], &[b"\x1b[?10", b"49l"], &[b"\x1b[?1000;1049l"], &[b"\x1b[?47l"]] {
            let mut model = Model::new(10, 40);
            assert_eq!(model.ended_picture(now), None, "no alternate screen yet");
            model.feed(b"shell\r\n\x1b[?1049h\x1b[?1000hpicture");
            let on_it = model.ended_picture(now).expect("died on it: that screen");
            assert!(has(&on_it, b"picture") && !has(&on_it, b"shell") && !has(&on_it, b"\x1b[?1049h") && !has(&on_it, b"\x1b[?1000h"));
            model.feed(b"-drawn");
            for part in leave {
                model.feed(part);
            }
            model.feed(b"resume lines");
            let picture = model.ended_picture(Instant::now()).expect("the picture it left");
            assert!(has(&picture, b"picture-drawn") && !has(&picture, b"resume lines") && !has(&picture, b"shell"), "{leave:?}");
            assert!(!has(&picture, b"\x1b[?1049h") && !has(&picture, b"\x1b[?1000h"), "normal-screen content, no modes");
            assert_eq!(model.ended_picture(Instant::now() + SHUTDOWN_LEAVE), None, "left long before the exit");
            assert!(has(&model.snapshot(), b"resume lines"), "the live snapshot is unchanged");
            model.feed(b"\x1b[?1049hagain");
            let again = model.ended_picture(Instant::now()).expect("back on the alternate screen");
            assert!(has(&again, b"again") && !has(&again, b"resume lines"));
        }
        // Longer than any fixed tail, split anywhere: the exact split from review, and a byte at a time.
        let long: &[u8] = b"\x1b[?1000;1002;1003;1049l";
        let byte_wise: Vec<&[u8]> = long.chunks(1).collect();
        for parts in [vec![&long[..20], &long[20..]], byte_wise] {
            let mut model = Model::new(10, 40);
            model.feed(b"\x1b[?1049hpicture-drawn");
            for part in parts {
                model.feed(part);
            }
            model.feed(b"resume lines");
            let picture = model.ended_picture(Instant::now()).expect("the picture it left");
            assert!(has(&picture, b"picture-drawn") && !has(&picture, b"resume lines"));
        }
        let mut model = Model::new(10, 40);
        model.feed(b"classic renderer\x1b[?1049l\x1b[?25l\x1b[?2004l");
        assert_eq!(model.ended_picture(Instant::now()), None, "never on the alternate screen");
    }

    #[test]
    fn holder_model_keeps_the_conversation_real_claude_code_leaves_on_exit() {
        let has = |hay: &[u8], needle: &[u8]| hay.windows(needle.len()).any(|w| w == needle);
        let mut model = Model::new(40, 120);
        model.feed(b"\x1b[?2004h\x1b[?2031h\x1b[?1004h\x1b[?1049h\x1b[2J\x1b[H\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?25l\x1b[H\r\x1b[6B> Reply with the single word READY.\r\x1b[1BREADY\x1b[40;1H\x1b[38;3H\x1b[?25h");
        for read in CLAUDE_EXIT_READS {
            model.feed(read);
        }
        let picture = model.ended_picture(Instant::now() + Duration::from_millis(470)).expect("the conversation it left");
        assert!(has(&picture, b"READY") && !has(&picture, b"Resume this session"));
        assert!(has(&model.snapshot(), b"Resume this session") && !has(&model.snapshot(), b"READY"), "live, the terminal shows the resume lines");
    }

    /// Bug 2026-10-07: the kept picture after `/exit` was the frame with the slash-command menu.
    #[test]
    fn holder_model_keeps_the_picture_from_before_the_command_that_ended_the_session() {
        let has = |hay: &[u8], needle: &[u8]| hay.windows(needle.len()).any(|w| w == needle);
        let secs = |s: f32| Duration::from_secs_f32(s);
        const LEAVE: &[u8] = b"\x1b[?1000l\x1b[?1049l\r\nResume this session with:";
        const MENU: &[u8] = b"\x1b[5;1H/exit     Exit the CLI\x1b[6;1H/context  Show context";
        let drawn = |t0: Instant| {
            let mut model = Model::new(10, 60);
            model.feed_at(b"\x1b[?1049h\x1b[2J\x1b[H\x1b[?1000h> question\r\nthe last answer\x1b[9;1H> ", t0);
            model
        };
        // Typed a key at a time, with a pause to read the menu before Enter.
        let t0 = Instant::now();
        let mut model = drawn(t0);
        for (i, key) in b"/exit".iter().enumerate() {
            let at = t0 + secs(10.0 + i as f32 * 0.2);
            model.typed(&[*key], at);
            model.feed_at(&[*key], at);
            model.feed_at(MENU, at);
        }
        model.typed(b"\x1b[<35;10;5M", t0 + secs(12.0)); // the mouse moved over the menu
        model.typed(b"\r", t0 + secs(14.0));
        model.feed_at(LEAVE, t0 + secs(14.01));
        let picture = model.ended_picture(t0 + secs(14.4)).expect("the picture before the command");
        assert!(has(&picture, b"the last answer") && has(&picture, b"> question"));
        assert!(!has(&picture, b"Exit the CLI") && !has(&picture, b"/exit") && !has(&picture, b"Resume this session"), "{:?}", String::from_utf8_lossy(&picture));
        assert!(!has(&picture, b"\x1b[?1049h") && !has(&picture, b"\x1b[?1000h"));

        // A whole line in one write (a paste, a teammate's relayed line).
        let mut model = drawn(t0);
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.feed_at(MENU, t0 + secs(10.0));
        model.feed_at(LEAVE, t0 + secs(10.2));
        assert!(!has(&model.ended_picture(t0 + secs(10.5)).unwrap(), b"Exit the CLI"));

        // Typed while an answer was still being drawn: no resting picture, so the last frame.
        let mut model = drawn(t0);
        model.typed(b"/exit", t0 + secs(1.0));
        model.feed_at(MENU, t0 + secs(1.0));
        model.typed(b"\r", t0 + secs(1.2));
        model.feed_at(LEAVE, t0 + secs(1.21));
        assert!(has(&model.ended_picture(t0 + secs(1.5)).unwrap(), b"Exit the CLI"));

        // The line was a question, answered; the session was then ended some other way (End, a
        // signal): the answer is the last picture, not the screen before the question.
        let mut model = drawn(t0);
        model.typed(b"another question\r", t0 + secs(10.0));
        model.feed_at(b"\x1b[3;1Ha newer answer", t0 + secs(12.0));
        model.feed_at(LEAVE, t0 + secs(20.0));
        assert!(has(&model.ended_picture(t0 + secs(20.3)).unwrap(), b"a newer answer"));

        // A question, a quick reply, then End within SENT_LEAVE of the Enter (review of #3818):
        // the End ended the session, so the reply is in the picture.
        let mut model = drawn(t0);
        model.typed(b"another question\r", t0 + secs(10.0));
        model.feed_at(b"\x1b[3;1Ha quick reply", t0 + secs(10.5));
        model.ending();
        model.typed(b"\r", t0 + secs(11.0));
        model.feed_at(LEAVE, t0 + secs(11.5));
        let picture = model.ended_picture(t0 + secs(11.8)).unwrap();
        assert!(has(&picture, b"a quick reply") && has(&picture, b"the last answer"));

        // A line left unsent for long is not the line being typed: `/exit` after it begins anew.
        let mut model = drawn(t0);
        model.typed(b"x", t0 + secs(10.0));
        model.feed_at(b"\x1b[9;3Hx-typed-long-ago", t0 + secs(10.0));
        model.typed(b"/exit\r", t0 + secs(100.0));
        model.feed_at(MENU, t0 + secs(100.0));
        model.feed_at(LEAVE, t0 + secs(100.1));
        let picture = model.ended_picture(t0 + secs(100.4)).unwrap();
        assert!(has(&picture, b"x-typed-long-ago") && !has(&picture, b"Exit the CLI"));
    }

    /// End session: the holder signals its provider. Claude Code 2.1.292 (fullscreen, 120x40 pty,
    /// captured 2026-10-07) on SIGHUP or SIGTERM writes what it writes on `/exit` (first read 3 ms
    /// after the signal) and exits 129 / 143 about 0.33 s later.
    #[test]
    fn holder_model_keeps_the_conversation_real_claude_code_leaves_when_signalled() {
        let has = |hay: &[u8], needle: &[u8]| hay.windows(needle.len()).any(|w| w == needle);
        const SIGNAL_READS: &[&[u8]] = &[
            b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l",
            b"\x1b[?25h",
            b"\x1b[?1004l\x1b[?1049l\x1b[>4m\x1b[>4m\x1b[?2031l\x1b[?2004l\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b7\x1b[r\x1b8",
            b"\x1b[?25h",
            b"\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[>4m\x1b[?1004l\x1b[?2031l\x1b[?2004l\x1b[?25h\x1b7\x1b[r\x1b8\x1b]0;\x07",
            b"\x1b[2m\x1b[22m\r\n\x1b[2mResume this session with:\x1b[22m\r\n\x1b[2mclaude --resume 1ea45316-3bb7-4447-b8f2-3d769e032c41\x1b[22m\r\n\x1b[2m\x1b[22m",
            b"\x1b[?25h",
        ];
        let t0 = Instant::now();
        let mut model = Model::new(40, 120);
        model.feed_at(b"\x1b[?1049h\x1b[2J\x1b[H\x1b[?1000h\x1b[?25l\x1b[H\r\x1b[6B> Reply with the single word READY.\r\x1b[1BREADY\x1b[40;1H\x1b[38;3H\x1b[?25h", t0);
        // A provider killed outright (SIGKILL after it ignored the signal) dies on the alternate screen.
        let killed = model.ended_picture(t0 + Duration::from_secs(60)).expect("the screen it died on");
        assert!(has(&killed, b"READY") && !has(&killed, b"\x1b[?1049h") && !has(&killed, b"\x1b[?1000h"));
        for read in SIGNAL_READS {
            model.feed_at(read, t0 + Duration::from_secs(45));
        }
        let picture = model.ended_picture(t0 + Duration::from_millis(45_330)).expect("the conversation it left");
        assert!(has(&picture, b"READY") && !has(&picture, b"Resume this session"));
    }

    #[test]
    fn holder_model_tells_probe_replies_from_typing() {
        for reply in [&b"\x1b[?62;22c"[..], b"\x1b[12;40R", b"\x1b]11;rgb:0/0/0\x07", b"\x1b[?1u", b"\x1bP>|xterm\x1b\\"] {
            assert!(is_probe_reply(reply), "{reply:?}");
        }
        for typed in [&b"\x1b[A"[..], b"hello", b"\x1b[200~x\x1b[201~", b"\x1b\r", b"\x1b["] {
            assert!(!is_probe_reply(typed), "{typed:?}");
        }
    }
}
