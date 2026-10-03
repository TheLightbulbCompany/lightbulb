//! The holder's copy of the screen: what a newly attached terminal is shown, and the answers
//! to the terminal queries the provider sends, so a session with no window still starts and
//! two windows never both answer.

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

pub struct Model {
    parser: vt100::Parser,
    /// The last bytes of the previous read: a query split across two reads is still answered.
    tail: Vec<u8>,
}
const TAIL: usize = 16;

/// How many times `needle` ends inside the new bytes (after the first `skip` bytes of `hay`).
fn completed(hay: &[u8], needle: &[u8], skip: usize) -> usize {
    hay.windows(needle.len()).enumerate().filter(|(i, w)| *w == needle && i + needle.len() > skip).count()
}

impl Model {
    pub fn new(rows: u16, cols: u16) -> Self {
        Self { parser: vt100::Parser::new(rows, cols, 5000), tail: Vec::new() }
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
        self.parser.process(bytes);
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
    fn holder_model_tells_probe_replies_from_typing() {
        for reply in [&b"\x1b[?62;22c"[..], b"\x1b[12;40R", b"\x1b]11;rgb:0/0/0\x07", b"\x1b[?1u", b"\x1bP>|xterm\x1b\\"] {
            assert!(is_probe_reply(reply), "{reply:?}");
        }
        for typed in [&b"\x1b[A"[..], b"hello", b"\x1b[200~x\x1b[201~", b"\x1b\r", b"\x1b["] {
            assert!(!is_probe_reply(typed), "{typed:?}");
        }
    }
}
