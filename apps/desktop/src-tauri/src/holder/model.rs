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
}
const TAIL: usize = 16;
/// A provider that exits this soon after leaving the alternate screen left it to shut down (Claude
/// Code's fullscreen renderer on `/exit`); a longer gap is a full-screen program that closed mid-session.
pub const SHUTDOWN_LEAVE: Duration = Duration::from_secs(5);
/// Mouse reporting off: a kept picture is read-only, and xterm selects no text while it is on.
const MOUSE_OFF: &[u8] = b"\x1b[?9;1000;1002;1003l";

/// Where in the new bytes (after the first `skip` bytes of `hay`) each sequence that leaves the
/// alternate screen starts: `ESC[?…l` naming 47 or 1049, the two the model acts on. One that began
/// in the bytes before starts at 0.
fn alternate_leaves(hay: &[u8], skip: usize) -> Vec<usize> {
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

/// How many times `needle` ends inside the new bytes (after the first `skip` bytes of `hay`).
fn completed(hay: &[u8], needle: &[u8], skip: usize) -> usize {
    hay.windows(needle.len()).enumerate().filter(|(i, w)| *w == needle && i + needle.len() > skip).count()
}

impl Model {
    pub fn new(rows: u16, cols: u16) -> Self {
        Self { parser: vt100::Parser::new(rows, cols, 5000), tail: Vec::new(), left_alternate: None }
    }

    /// Feed provider output. Returns what to write back to the pty: the probe answers.
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<u8> {
        let skip = self.tail.len();
        let mut seen = std::mem::take(&mut self.tail);
        seen.extend_from_slice(bytes);
        let mut answers = Vec::new();
        for (query, answer) in PROBES {
            for _ in 0..completed(&seen, query, skip) {
                answers.extend_from_slice(answer);
            }
        }
        // Stop just before each leave of the alternate screen, to keep the picture it ends.
        let mut done = 0;
        for cut in alternate_leaves(&seen, skip) {
            self.parser.process(&bytes[done..cut]);
            done = cut;
            let screen = self.parser.screen();
            if screen.alternate_screen() {
                let mut picture = screen.contents_formatted();
                picture.extend_from_slice(MOUSE_OFF);
                self.left_alternate = Some((picture, Instant::now()));
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
    /// picture is only read. None when the normal screen is the last picture.
    pub fn ended_picture(&self, exited: Instant) -> Option<Vec<u8>> {
        let (picture, left) = self.left_alternate.as_ref()?;
        (!self.parser.screen().alternate_screen() && exited.saturating_duration_since(*left) < SHUTDOWN_LEAVE).then(|| picture.clone())
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

#[cfg(test)]
mod tests {
    use super::*;

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
            assert_eq!(model.ended_picture(now), None, "still on it: the snapshot is that screen already");
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
            assert_eq!(model.ended_picture(Instant::now()), None, "back on the alternate screen");
        }
        let mut model = Model::new(10, 40);
        model.feed(b"classic renderer\x1b[?1049l\x1b[?25l\x1b[?2004l");
        assert_eq!(model.ended_picture(Instant::now()), None, "never on the alternate screen");
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
