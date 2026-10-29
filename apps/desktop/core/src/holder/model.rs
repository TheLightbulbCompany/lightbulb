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
    /// Once ended (`end`): how many of the newest lines above the screen the kept picture shows
    /// again. `history` leaves them out.
    shown_again: usize,
    /// The provider is Codex in its inline mode (`run::CODEX_INLINE`): it erases the saved lines too.
    codex: bool,
    /// Where the output read so far stands (`process`).
    at: At,
    /// The parameters so far of the control sequence the output stands in.
    csi: Vec<u8>,
    /// The last row of the scroll region, while that region starts at the first row and ends above
    /// the last one: lines that leave its top are lines a terminal keeps (`process`).
    region: Option<u16>,
    /// The first bytes of a character the last read ended inside, on that region's last row: not
    /// fed yet, since whether it wraps there (and scrolls the region) depends on its width.
    carry: Vec<u8>,
    /// How many of the lines above the screen `scrolled` has already handed out.
    taken: usize,
    /// `ESC[3J` dropped the lines above since `scrolled` last ran.
    erased: bool,
}
/// Where a byte stream stands between its control sequences, as far as `Model::process` reads it.
#[derive(Clone, Copy, PartialEq)]
enum At {
    Ground,
    /// After ESC, and its intermediates.
    Esc,
    /// Inside `ESC[`.
    Csi,
    /// Inside an OSC string: BEL ends it.
    Osc,
    /// Inside a DCS, SOS, PM or APC string: only ST (`ESC\`) ends it.
    Str,
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
/// Lightbulb's band as the Claude Code mod draws it above the prompt
/// (`packages/claude-plugin/lightbulb/hooks/register.tsx` and `state.ts`; keep these with them):
/// one line that opens with one of BAND_LEADS (then, live, the viewers' names), has one of
/// BAND_BUTTONS, and ends with one of BAND_HINTS unless someone asks to join. Then, in this order and
/// each only when it applies: the first request's line (a name, one of ASK_VERBS, "(+N more)" when
/// others wait, ASK_PARTS) and WAITING. A terminal narrower than a line wraps it over rows (the
/// shortest band is 44 columns); the engine's "[-]" ends the band's first row. How it wraps:
/// `band_columns`.
// The first two leads and buttons are the mod's before 2026-10-08: a session started then still draws them.
const BAND_LEADS: [&str; 6] = ["○ Sharing paused", "● Shared as a transcript", "○ Only you", "● Shared in Lightbulb", "● Live in Lightbulb", "● Live · "];
const BAND_BUTTONS: [&str; 4] = ["[ Pause ]", "[ Resume sharing ]", "[ Stop sharing ]", "[ Share to Lightbulb ]"];
const BAND_HINTS: [&str; 2] = ["ctrl+x tab, then p", "ctrl+x tab, then s"];
/// A band that is one line with no button.
const BAND_ALONE: [&str; 2] = ["○ Press ← once to go live", "○ Stopping… teammates may still see earlier output"];
const ASK_VERBS: [&str; 2] = [" asked to join", " wants to pick this up · conversation + uncommitted changes"];
const ASK_PARTS: [&str; 3] = ["[ Allow ]", "[ Decline ]", "ctrl+x tab, then a or d · or click"];
const WAITING: &str = "Waiting on you here or in Lightbulb";

/// Text without its spaces: a wrapped line reads the same whatever rows its words fell on.
fn squash(text: &str) -> String {
    text.chars().filter(|c| !c.is_whitespace()).collect()
}

/// How many rows from `at` hold one whole line of the band. `fits` reads the rows so far, joined:
/// Some(true) a whole line, Some(false) the start of one, None not one. The longest whole one
/// counts (a hint wrapped under its button belongs to the line). No limit on the rows: the mod
/// names every viewer, and nothing caps how many watch. The search ends where the line does, at
/// a blank row or at the end of the screen. 0: none.
fn band_line(rows: &[String], at: usize, fits: impl Fn(&str) -> Option<bool>) -> usize {
    let (mut joined, mut whole) = (String::new(), 0);
    for (n, row) in rows.iter().skip(at).enumerate() {
        let row = squash(row);
        let row = row.strip_suffix("[-]").unwrap_or(&row);
        if row.is_empty() {
            break;
        }
        joined.push_str(row);
        match fits(&joined) {
            Some(true) => whole = n + 1,
            Some(false) => {}
            None => break,
        }
    }
    whole
}

/// `joined` so far is `line`, or starts it.
fn so_far(joined: &str, line: &str) -> Option<bool> {
    let line = squash(line);
    line.starts_with(joined).then_some(line == joined)
}

/// `joined` so far is one of `lines`, or starts one.
fn so_far_any(joined: &str, lines: &[&str]) -> Option<bool> {
    lines.iter().filter_map(|line| so_far(joined, line)).max()
}

fn fits_band(joined: &str) -> Option<bool> {
    let lead = BAND_LEADS.iter().map(|lead| squash(lead)).find(|lead| joined.starts_with(lead.as_str()) || lead.starts_with(joined))?;
    if joined.len() < lead.len() {
        return Some(false);
    }
    // The names may still be going: no button yet.
    let Some(after) = BAND_BUTTONS.iter().find_map(|button| joined.split_once(squash(button).as_str())).map(|(_, after)| after) else { return Some(false) };
    // (With a request under it the band has no hint.)
    if after.is_empty() { Some(true) } else { so_far_any(after, &BAND_HINTS) }
}

fn fits_ask(joined: &str) -> Option<bool> {
    // Until its last word a request's line is only told from other text by where it stands.
    let Some(said) = joined.strip_suffix(squash(&ASK_PARTS.concat()).as_str()) else { return Some(false) };
    asks(said).then_some(true)
}

/// `said`, without spaces, is all a request says before its buttons: a name, one of ASK_VERBS,
/// "(+N more)" when others wait. A name with a verb in it is two lines read as one.
fn asks(said: &str) -> bool {
    let said = match said.strip_suffix("more)").and_then(|rest| rest.rsplit_once("(+")) {
        Some((before, count)) if !count.is_empty() && count.chars().all(|c| c.is_ascii_digit()) => before,
        _ => said,
    };
    let verbs: Vec<String> = ASK_VERBS.iter().map(|verb| squash(verb)).collect();
    verbs.iter().any(|verb| said.strip_suffix(verb.as_str()).is_some_and(|name| !name.is_empty() && !verbs.iter().any(|verb| name.contains(verb.as_str()))))
}

/// `said`, without spaces, is all a band says before its button: one of BAND_LEADS, and live the
/// viewers' names, "watching" and, when someone types, "·", their names and "typing".
/// A live band ends with "· N via link" while people watch on the session's public link; with no
/// teammate watching, that count is all it says after "● Live".
fn opens_band(said: &str) -> bool {
    let (said, by_link) = match said.rsplit_once('·') {
        Some((before, count)) if count.strip_suffix("vialink").is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit())) => (before, true),
        _ => (said, false),
    };
    BAND_LEADS.iter().map(|lead| squash(lead)).any(|lead| {
        if !lead.ends_with('·') {
            return !by_link && said == lead;
        }
        if by_link && lead.strip_suffix('·') == Some(said) {
            return true;
        }
        let Some((names, typing)) = said.strip_prefix(lead.as_str()).and_then(|rest| rest.split_once("watching")) else { return false };
        !names.is_empty() && !typing.contains("watching") && (typing.is_empty() || typing.strip_prefix('·').and_then(|rest| rest.strip_suffix("typing")).is_some_and(|names| !names.is_empty()))
    })
}

/// How many rows from `at` hold one line of the band where the terminal is narrower than the
/// line. The line is a row of parts side by side (the text, each button, the hint), and each part
/// wraps inside its own column: prod 2026-10-08 at 60 columns,
/// "Isol8 Admin    [ Allow ][ Decline ] ctrl+x tab, then a" over
/// "asked to join                      or d · or click". So a row is the next piece of each part,
/// left to right, and a part has a piece in every row from the first until it is all there.
/// `said` knows the first part when it is whole; `fixed` are the parts after it. The longest
/// such run of rows counts (the text may be the tallest column); it ends at a blank row or at
/// the end of the screen. 0: none.
fn band_columns(rows: &[String], at: usize, fixed: &[&str], said: impl Fn(&str) -> bool) -> usize {
    let fixed: Vec<String> = fixed.iter().map(|part| squash(part)).collect();
    // Each way the rows so far can be read: the first part's text, whether it has ended, and how much of each fixed part is read.
    let mut ways = vec![(String::new(), false, vec![0usize; fixed.len()])];
    let mut whole = 0;
    for (n, row) in rows.iter().skip(at).enumerate() {
        let row = squash(row);
        let row = row.strip_suffix("[-]").unwrap_or(&row);
        if row.is_empty() {
            break;
        }
        let mut next = Vec::new();
        for (text, ended, read) in &ways {
            for cut in (0..=row.len()).filter(|cut| row.is_char_boundary(*cut)) {
                // The text starts on the first row, and has no row after one it left empty.
                if (cut == 0 && n == 0) || (cut > 0 && *ended) {
                    continue;
                }
                let mut reads = Vec::new();
                spread(&row[cut..], &fixed, read.clone(), 0, &mut reads);
                next.extend(reads.into_iter().map(|read| (format!("{text}{}", &row[..cut]), cut == 0, read)));
            }
        }
        if next.is_empty() {
            break;
        }
        ways = next;
        if ways.iter().any(|(text, _, read)| said(text.as_str()) && read.iter().zip(&fixed).all(|(read, part)| *read == part.len())) {
            whole = n + 1;
        }
    }
    whole
}

/// Every way `rest` is the next piece of each of `fixed` from `part` on, in order, into `out`
/// (how much of each is read then). A part not all read has a piece of one character or more.
fn spread(rest: &str, fixed: &[String], read: Vec<usize>, part: usize, out: &mut Vec<Vec<usize>>) {
    let Some(text) = fixed.get(part) else {
        if rest.is_empty() {
            out.push(read);
        }
        return;
    };
    let left = &text[read[part]..];
    if left.is_empty() {
        spread(rest, fixed, read, part + 1, out);
        return;
    }
    let mut len = 0;
    for (a, b) in rest.chars().zip(left.chars()) {
        if a != b {
            break;
        }
        len += a.len_utf8();
        let mut read = read.clone();
        read[part] += len;
        spread(&rest[len..], fixed, read, part + 1, out);
    }
}

/// `picture`, a `rows` x `cols` screen, without Lightbulb's band. On a kept picture it says an
/// ended session is live and offers buttons that do nothing. The rows under it move up. A picture
/// with no band is returned as it is.
// ponytail: known by its text, since the mod has no channel to the holder; a refused decision's
// red line under it (any text) stays.
fn without_band(picture: Vec<u8>, (rows, cols): (u16, u16)) -> Vec<u8> {
    let mut screen = vt100::Parser::new(rows, cols, 0);
    screen.process(&picture);
    let text: Vec<String> = screen.screen().rows(0, cols).collect();
    // The last one: the band is under the conversation, and the conversation may quote one.
    // Each line either wrapped as one text or part by part (`band_columns`); a band with a request under it has no hint.
    let band_at = |at: usize| {
        let columns = BAND_BUTTONS
            .iter()
            .flat_map(|button| BAND_HINTS.map(|hint| vec![*button, hint]).into_iter().chain([vec![*button]]))
            .map(|fixed| band_columns(&text, at, &fixed, opens_band));
        let line = band_line(&text, at, fits_band).max(band_line(&text, at, |joined| so_far_any(joined, &BAND_ALONE)));
        columns.fold(line, usize::max)
    };
    let Some((first, band)) = (0..text.len()).rev().map(|at| (at, band_at(at))).find(|(_, band)| *band > 0) else { return picture };
    let ask = band_line(&text, first + band, fits_ask).max(band_columns(&text, first + band, &ASK_PARTS, asks));
    let count = band + ask + band_line(&text, first + band + ask, |joined| so_far(joined, WAITING));
    let (row, col) = screen.screen().cursor_position();
    let row = match usize::from(row) {
        at if at >= first + count => at - count,
        at => at.min(first),
    };
    screen.process(format!("\x1b[{};1H\x1b[{count}M\x1b[{};{}H", first + 1, row + 1, col + 1).as_bytes());
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

/// How many bytes the UTF-8 character that starts with `lead` has.
fn char_len(lead: u8) -> usize {
    match lead {
        0..=0x7f => 1,
        0xc0..=0xdf => 2,
        0xe0..=0xef => 3,
        _ => 4,
    }
}

/// Whether `bytes` end inside a UTF-8 character.
fn mid_char(bytes: &[u8]) -> bool {
    let last = &bytes[bytes.len().saturating_sub(4)..];
    last.iter().rposition(|b| b & 0xc0 != 0x80).is_some_and(|at| last[at] >= 0xc0 && char_len(last[at]) > last.len() - at)
}

/// Whether the character `ch` (all its bytes) goes to the next row when written at column `col`
/// of `cols`: vt100's own rule (`Screen::text`), with its own width for that character.
fn wraps(ch: &[u8], col: u16, cols: u16) -> bool {
    let width = if ch.len() == 1 {
        1
    } else {
        let mut probe = vt100::Parser::new(1, 4, 0);
        probe.process(ch);
        probe.screen().cursor_position().1
    };
    width > 0 && col > cols.saturating_sub(width)
}

/// Lines the model keeps above its screen: what `Model::history` can give a new viewer.
const SCROLLBACK: usize = 5000;
/// Erase the saved lines: a terminal drops everything above its screen. vt100 does not act on it.
const ERASE_SAVED: &[u8] = b"\x1b[3J";

impl Model {
    pub fn new(rows: u16, cols: u16) -> Self {
        Self { parser: vt100::Parser::new(rows, cols, SCROLLBACK), tail: Vec::new(), left_alternate: None, partial_csi: Vec::new(),
            last_output: None, typed: None, resting: None, ending: false, classic: false, shown_again: 0,
            codex: false, at: At::Ground, csi: Vec::new(), region: None, carry: Vec::new(), taken: 0, erased: false }
    }

    /// The provider is Codex in its inline mode.
    pub fn inline_codex(&mut self) {
        self.codex = true;
    }

    /// Feed the screen. A terminal keeps a line that scrolls off the top of the scroll region
    /// whenever that region starts at the first row (xterm.js `BufferService.scroll`: "scrollTop is
    /// non-zero which means no line will be going to the scrollback"); vt100 0.16 keeps it only
    /// while no region is set (`Grid::scroll_up`), and has no hook to see one go. Codex's inline
    /// mode writes every finished line that way: region from the first row to the row above its
    /// prompt, cursor on the region's last row, `CR LF`, the line (h14b, prod 2026-10-08: all 2127
    /// lines above the screen came through 209 such regions; a late joiner's 28 wrong first rows
    /// were those lines missing from the model). So this follows the output just far enough to know
    /// that region (`ESC[1;<n>r`), and each scroll of it is fed as what a terminal does with it
    /// (`scroll_region`). The two things that scroll in vt100 are a line feed on the region's last
    /// row and a character that wraps there; `ESC[<n>S` moves the region without saving in a
    /// terminal too.
    // ponytail: a line feed inside a control sequence (C0 executes there) is fed as it is, and
    // that line is not kept; no program writes one.
    fn process(&mut self, bytes: &[u8]) {
        let joined;
        let bytes = if self.carry.is_empty() {
            bytes
        } else {
            joined = [std::mem::take(&mut self.carry).as_slice(), bytes].concat();
            &joined[..]
        };
        let mut done = 0;
        for (i, &b) in bytes.iter().enumerate() {
            match self.at {
                At::Ground if b == 0x1b => self.at = At::Esc,
                At::Ground => {
                    let Some(bottom) = self.region else { continue };
                    let feed = matches!(b, 0x0a..=0x0c);
                    // (Else only the first byte of a character: no control, no UTF-8 continuation.)
                    if !feed && (b < 0x20 || b == 0x7f || (0x80..0xc0).contains(&b)) {
                        continue;
                    }
                    self.parser.process(&bytes[done..i]);
                    done = i;
                    let screen = self.parser.screen();
                    let (cols, (row, col)) = (screen.size().1, screen.cursor_position());
                    if screen.alternate_screen() || row != bottom {
                        continue;
                    }
                    if feed {
                        // The line feed is this scroll: the cursor stays where it is.
                        self.scroll_region(bottom, col);
                        done = i + 1;
                    } else {
                        let Some(ch) = bytes.get(i..i + char_len(b)) else {
                            // The read ends inside the character (a wide one on the last column
                            // wraps, a narrow one does not): it waits for its other bytes, 3 at most.
                            self.carry = bytes[i..].to_vec();
                            return;
                        };
                        if wraps(ch, col, cols) {
                            // The character then starts the region's new last row.
                            self.scroll_region(bottom, 0);
                        }
                    }
                }
                At::Esc => {
                    self.at = match b {
                        b'[' => At::Csi,
                        b']' => At::Osc,
                        b'P' | b'X' | b'^' | b'_' => At::Str,
                        0x18 | 0x1a => At::Ground,
                        // (Another ESC, an intermediate, or a C0 control, which runs and leaves the sequence open.)
                        0x00..=0x2f | 0x7f => At::Esc,
                        _ => At::Ground,
                    };
                    self.csi.clear();
                    // RIS: a new terminal.
                    if b == b'c' {
                        self.region = None;
                    }
                }
                At::Csi => match b {
                    0x1b => self.at = At::Esc,
                    0x18 | 0x1a => self.at = At::Ground,
                    0x40..=0x7e => {
                        self.at = At::Ground;
                        let params = std::mem::take(&mut self.csi);
                        if b == b'r' {
                            // DECSTBM as vt100 reads it (`canonicalize_params_decstbm`, `Grid::set_scroll_region`),
                            // once vt100 has it. Anything else that ends in `r` leaves the region unknown.
                            self.parser.process(&bytes[done..=i]);
                            done = i + 1;
                            let rows = u32::from(self.size().0);
                            let plain = params.iter().all(|p| p.is_ascii_digit() || *p == b';');
                            // (Empty is 0, the default; too large for vt100's u16 is past every row.)
                            let number = |p: &[u8]| if p.is_empty() { 0 } else { std::str::from_utf8(p).ok().and_then(|p| p.parse::<u32>().ok()).unwrap_or(u32::MAX) };
                            let mut given = params.split(|p| *p == b';').map(number);
                            let top = given.next().filter(|top| *top != 0).unwrap_or(1);
                            let bottom = given.next().filter(|bottom| *bottom != 0).unwrap_or(rows);
                            let kept = plain && !self.parser.screen().alternate_screen() && top == 1 && bottom > 1 && bottom < rows;
                            self.region = kept.then(|| (bottom - 1) as u16);
                        } else if matches!(b, b'h' | b'l') && params.first() == Some(&b'?') && params[1..].split(|p| *p == b';').any(|p| p == b"47" || p == b"1047" || p == b"1049") {
                            // The other screen has its own region.
                            self.region = None;
                        }
                    }
                    0x20..=0x3f if self.csi.len() < PARTIAL_CSI_MAX => self.csi.push(b),
                    _ => {}
                },
                At::Osc | At::Str => match b {
                    0x1b => self.at = At::Esc,
                    0x18 | 0x1a => self.at = At::Ground,
                    0x07 if self.at == At::Osc => self.at = At::Ground,
                    _ => {}
                },
            }
        }
        self.parser.process(&bytes[done..]);
    }

    /// The top line of the region that ends on row `bottom` leaves it for the saved lines, the rows
    /// under it move up and the region's last row is blank, as in a terminal. Made of what vt100
    /// does save: with no region, a line feed on the last row keeps the top line and moves every
    /// row up; a line inserted where the region ended puts the rows under the region back and
    /// leaves that row blank. Then the region again, and the cursor on its last row at `col`.
    // ponytail: a cursor waiting past the last column (a full row, then a bare line feed) comes
    // back on the last column; Codex sends CR LF.
    fn scroll_region(&mut self, bottom: u16, col: u16) {
        let (rows, cols) = self.size();
        let (last, col) = (bottom + 1, col.min(cols.saturating_sub(1)) + 1);
        self.parser.process(format!("\x1b[r\x1b[{rows};1H\n\x1b[{last};1H\x1b[L\x1b[1;{last}r\x1b[{last};{col}H").as_bytes());
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
    /// same screen (`CLAUDE_CLASSIC_SIGNAL_READS`). So its picture is taken here, and `end`
    /// counts the lines the way out pushes above the screen.
    pub fn ending(&mut self) {
        self.ending = true;
        self.resting = (self.classic && !self.parser.screen().alternate_screen()).then(|| self.rest(Instant::now()));
    }

    /// `ESC[3J` on the normal screen: the lines above it are gone from every terminal (Claude
    /// Code's classic renderer clears the terminal and prints the conversation again). The model
    /// starts over from its own screen, which is all a terminal has left.
    // ponytail: the scroll region and the saved cursor do not survive this; classic Claude Code
    // sets neither while it runs, and Codex drops its region (`ESC[r`) before it erases. Keep
    // them if a provider that does not ever needs this.
    fn erase_saved(&mut self) {
        if self.parser.screen().alternate_screen() {
            return;
        }
        let (rows, cols) = self.size();
        let mut parser = vt100::Parser::new(rows, cols, SCROLLBACK);
        parser.process(&self.snapshot());
        self.parser = parser;
        self.region = None;
        self.erased = true;
    }

    pub fn screen(&self) -> &vt100::Screen {
        self.parser.screen()
    }

    /// Rows that left the top of the normal screen since the last call, oldest first (each one row
    /// of `rows_formatted`), and whether ESC[3J dropped the lines above since then (the count
    /// restarts). The relay pushes them into viewers' scrollback before each diff.
    // ponytail: more than SCROLLBACK - taken rows between two calls lose the oldest; the parser is
    // rebuilt at half the cap so the count never saturates (which also empties what `history`
    // can give, so only a model that is not the holder's calls this). A Codex region is set again
    // after the rebuild (Codex review of #4035: skipping the rebuild while one was set saturated
    // the count, and no later row reached a viewer). Lost there: any region not anchored at row 1,
    // and the saved cursor, which `region` does not track.
    pub fn scrolled(&mut self) -> (bool, Vec<Vec<u8>>) {
        let erased = std::mem::take(&mut self.erased);
        if erased {
            self.taken = 0;
        }
        if self.parser.screen().alternate_screen() {
            return (erased, vec![]);
        }
        let (rows_n, cols) = self.size();
        let screen = self.parser.screen_mut();
        screen.set_scrollback(usize::MAX);
        let above = screen.scrollback();
        let rows: Vec<Vec<u8>> = (1..=above.saturating_sub(self.taken))
            .rev()
            .filter_map(|back| {
                screen.set_scrollback(back);
                screen.rows_formatted(0, cols).next()
            })
            .collect();
        screen.set_scrollback(0);
        self.taken = above;
        // (Only between sequences and characters: a new parser would print the rest of one as
        // text. Else at a later call; the other half of the cap is the room for that.)
        if above >= SCROLLBACK / 2 && self.at == At::Ground && self.carry.is_empty() && !mid_char(&self.tail) {
            let mut parser = vt100::Parser::new(rows_n, cols, SCROLLBACK);
            parser.process(&self.snapshot());
            if let Some(bottom) = self.region {
                // DECSTBM homes the cursor: put it back.
                let (row, col) = self.parser.screen().cursor_position();
                parser.process(format!("\x1b[1;{}r\x1b[{};{}H", bottom + 1, row + 1, col + 1).as_bytes());
            }
            self.parser = parser;
            self.taken = 0;
        }
        (erased, rows)
    }

    /// The lines above the screen, oldest first, as bytes for an empty terminal: they end up in
    /// its scrollback, above a blank screen for the snapshot that follows. Whole lines, the newest
    /// that fit `max` bytes; empty when nothing is above the screen. Unlike a tail of the output
    /// log, it starts clean anywhere: the classic renderer redraws with cursor moves relative to
    /// where it was, so its log read from the middle draws over the wrong rows.
    pub fn history(&mut self, max: usize) -> Vec<u8> {
        let ((rows, cols), again) = (self.size(), self.shown_again);
        let screen = self.parser.screen_mut();
        screen.set_scrollback(usize::MAX);
        let above = screen.scrollback();
        // (After the end: not the lines the kept picture shows again.)
        let again = again.min(above);
        // Line feeds after the last line: they push it, too, off the empty terminal's screen.
        let feeds = usize::from(rows).saturating_sub(1);
        let (mut lines, mut size) = (Vec::new(), feeds);
        for back in again + 1..=above {
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
        // (classic Claude Code, inline Codex) just after each erase of the saved lines.
        let mut done = 0;
        let mut csi = std::mem::take(&mut self.partial_csi);
        let carried = csi.len();
        csi.extend_from_slice(bytes);
        self.partial_csi = partial_csi(&csi).to_vec();
        let mut cuts: Vec<(usize, bool)> = alternate_leaves(&csi, carried).into_iter().map(|cut| (cut, true)).collect();
        if self.classic || self.codex {
            cuts.extend(ends(&seen, ERASE_SAVED, skip).map(|cut| (cut, false)));
            cuts.sort_unstable();
        }
        for (cut, leave) in cuts {
            self.process(&bytes[done..cut]);
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
        self.process(&bytes[done..]);
        for _ in 0..completed(&seen, CPR, skip) {
            let (row, col) = self.parser.screen().cursor_position();
            answers.extend_from_slice(format!("\x1b[{};{}R", row + 1, col + 1).as_bytes());
        }
        self.tail = seen[seen.len().saturating_sub(TAIL)..].to_vec();
        answers
    }

    pub fn resize(&mut self, rows: u16, cols: u16) {
        self.parser.screen_mut().set_size(rows, cols);
        // (vt100 keeps or drops the region by its own rule: unknown until the provider sets it again.)
        self.region = None;
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
    /// Enter; 2.1.293 took 0.39 s), the picture is the whole screen at rest before that line. The
    /// same for the picture taken when the holder ended it (`ending`), however long the way out took.
    /// The way out pushed that screen's top lines above it, so the picture shows them again: the
    /// second value is how many (prod 2026-10-08: a picture drawn without them left that many blank
    /// rows at the bottom of every viewer's terminal, 18 of 30 after `/exit`).
    ///
    /// Never with Lightbulb's band (`without_band`). None when the final screen is the last picture.
    fn kept(&self, exited: Instant) -> Option<(Vec<u8>, usize)> {
        let screen = self.parser.screen();
        let size = screen.size();
        if screen.alternate_screen() {
            return Some((without_band(self.picture(), size), 0));
        }
        let within = |at: Instant, limit: Duration| exited.saturating_duration_since(at) < limit;
        if let Some((picture, _)) = self.left_alternate.as_ref().filter(|(_, left)| within(*left, SHUTDOWN_LEAVE)) {
            return Some((without_band(picture.clone(), size), 0));
        }
        let rested = self.resting.as_ref().filter(|r| self.ending || (within(r.began, RESTING_FOR) && r.sent.is_some_and(|at| within(at, SHUTDOWN_LEAVE))))?;
        let mut since = rested.since.as_ref()?.screen().clone();
        since.set_scrollback(usize::MAX);
        let gone = since.scrollback();
        // (A whole screen gone: its lines are a viewer's history already, and the final screen follows them.)
        (gone < usize::from(size.0)).then(|| (without_band(rested.picture.clone(), size), gone))
    }

    /// The provider exited at `exited`: the picture to keep (`kept`). From here `history` is the
    /// lines above that picture: a terminal drawn from both shows the session as it rested, each
    /// line once, with the picture where the screen was. A terminal that watched the way out has
    /// the picture's top lines above its screen already, so it starts over from both
    /// (`starts_over`, `screen::pump`).
    pub fn end(&mut self, exited: Instant) -> Option<Vec<u8>> {
        let (picture, again) = self.kept(exited)?;
        self.shown_again = again;
        Some(picture)
    }

    /// After `end`, when the kept picture shows again lines a terminal that watched the way out has
    /// above its screen: how many bytes all of `history` is. That terminal may hold more lines than
    /// one burst carries, so it starts over only from a history that is whole.
    pub fn starts_over(&mut self) -> Option<usize> {
        (self.shown_again > 0).then(|| self.history(usize::MAX).len())
    }

    #[cfg(test)]
    fn ended_picture(&self, exited: Instant) -> Option<Vec<u8>> {
        self.kept(exited).map(|(picture, _)| picture)
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
/// Not `cfg(test)`: the app's `local_sessions::screen` tests read it, and a dependency is never
/// compiled with `cfg(test)`.
#[doc(hidden)]
pub const CLAUDE_EXIT_READS: &[&[u8]] = &[
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

    /// How many lines a viewer's terminal has above its screen.
    fn above(term: &mut vt100::Parser) -> usize {
        term.screen_mut().set_scrollback(usize::MAX);
        let above = term.screen().scrollback();
        term.screen_mut().set_scrollback(0);
        above
    }

    /// `lines` without Lightbulb's band, which a kept picture never has: its last row with the Pause button.
    fn bandless(lines: &[String]) -> Vec<String> {
        let band = lines.iter().rposition(|l| l.contains("[ Pause ]")).expect("the band is drawn at rest");
        lines.iter().enumerate().filter(|(i, _)| *i != band).map(|(_, l)| l.clone()).collect()
    }

    /// What a viewer's terminal shows of an ended session: one that opens it later, and one that watched
    /// it end (it starts over, `screen::pump`). The holder's lines above the picture, then the picture
    /// as the web view draws it.
    fn drawn_after_the_end(model: &mut Model, picture: &[u8]) -> vt100::Parser {
        let (rows, cols) = model.size();
        let mut term = vt100::Parser::new(rows, cols, 1000);
        term.process(&model.history(1 << 20));
        term.process(WEB_CLEAR);
        term.process(picture);
        term
    }

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
            let (mut rested, mut exited) = ((vec![], 0), t0);
            for (i, (at, kind, data)) in capture.iter().enumerate() {
                if i == slash {
                    rested = (lines(&mut viewer), above(&mut viewer));
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
        let (mut model, mut viewer, (rested, rested_above), exited) = run(true);
        let said = |lines: &[String], text: &str| lines.iter().any(|l| l.contains(text));
        assert!(said(&rested, "Reply with the single word BRAVO.") && !said(&rested, "❯ /exit") && !said(&rested, "Exit the CLI"));
        let last = lines(&mut viewer);
        assert!(said(&last, "❯ /exit") && said(&last, "Exit the CLI") && said(&last, "Resume this session with:") && last.len() > rested.len(), "the final screen has the menu and the resume lines");
        assert!(above(&mut viewer) > rested_above, "the way out pushed lines of the screen at rest above it");
        let picture = model.end(exited).expect("the screen at rest before /exit");
        assert!(!picture.windows(8).any(|w| w == b"\x1b[?1049h"));
        assert_eq!(model.starts_over(), Some(model.history(1 << 20).len()), "whoever watched the way out starts over, from every line above the picture");
        // Prod 2026-10-08: the picture came without the lines the way out had pushed up, so 18 of a
        // viewer's 30 rows were blank under it, and it still showed the band ("● Live · … [ Pause ]").
        let mut late = drawn_after_the_end(&mut model, &picture);
        assert_eq!(lines(&mut late), bandless(&rested), "history and picture are the session as it rested: nothing twice, nothing missing, no menu, no band");
        assert_eq!(above(&mut late), rested_above, "the picture is the whole screen at rest, where that screen was");
        // The picture alone.
        let mut alone = vt100::Parser::new(40, 120, 0);
        alone.process(WEB_CLEAR);
        alone.process(&picture);
        let shown = lines(&mut alone);
        assert!(said(&shown, "Reply with the single word BRAVO.") && !said(&shown, "❯ /exit") && !said(&shown, "Exit the CLI") && !said(&shown, "Resume this session") && !said(&shown, "[ Pause ]"));
        // Live, the terminal still shows what the provider drew last.
        let mut live = vt100::Parser::new(40, 120, 0);
        live.process(&model.snapshot());
        assert!(said(&lines(&mut live), "Exit the CLI"));
        // The exit came long after that Enter: something else ended it, and the final screen stays.
        assert_eq!(model.ended_picture(exited + SHUTDOWN_LEAVE), None);
        // Any other program on the normal screen (Codex with `--no-alt-screen`, a shell): as before.
        let (mut other, _, _, exited) = run(false);
        assert_eq!(other.end(exited), None);
        assert_eq!(other.starts_over(), None);
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
        let (rested, rested_above) = (lines(&mut viewer), above(&mut viewer));
        let said = |lines: &[String], text: &str| lines.iter().any(|l| l.contains(text));
        assert!(said(&rested, "Reply with the single word BRAVO.") && !said(&rested, "Resume this session"));
        let end = t0 + Duration::from_millis(events[slash].0);
        model.ending();
        for read in CLAUDE_CLASSIC_SIGNAL_READS {
            model.feed_at(read, end);
            viewer.process(read);
        }
        assert!(said(&lines(&mut viewer), "Resume this session with:"), "the final screen has the resume lines");
        let picture = model.end(end + Duration::from_millis(440)).expect("the screen End found");
        let mut late = drawn_after_the_end(&mut model, &picture);
        assert_eq!(lines(&mut late), bandless(&rested), "nothing twice, nothing missing, no resume lines, no band");
        assert_eq!(above(&mut late), rested_above, "the picture is the whole screen End found, where that screen was");
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

        // Two lines left the top on the way out: the picture is still the whole screen at rest, and
        // the lines above it no longer have those two.
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.feed_at(MENU, t0 + secs(10.0));
        model.feed_at(b"\r\n  /more\r\n  /more\r\n  /more", t0 + secs(10.0));
        model.feed_at(RESUME, t0 + secs(10.01));
        assert!(!model.history(1 << 20).is_empty() && model.starts_over().is_none());
        assert_eq!(shown(model.end(t0 + secs(10.4))), Some(rest.clone()));
        assert!(model.history(1 << 20).is_empty() && model.starts_over() == Some(0), "the two lines are in the picture, not above it too");

        // The whole screen left the top: the final screen is the picture.
        let mut model = drawn();
        model.typed(b"/exit\r", t0 + secs(10.0));
        model.feed_at(&b"\r\nline".repeat(16), t0 + secs(10.0));
        assert_eq!(model.ended_picture(t0 + secs(10.4)), None);

        // End session at an empty prompt (real reads; prod 2026-10-07: the kept picture ended with the
        // resume lines). The picture is the whole screen as End found it, the quick reply included,
        // though the way out pushed three of its lines above it (40 columns: the resume command wraps);
        // however long the way out took (a kill after 5 s).
        let mut model = drawn();
        model.typed(b"another question\r", t0 + secs(10.0));
        model.feed_at(b"\r\na quick reply\r\n> ", t0 + secs(10.5));
        model.ending();
        model.typed(b"typed after End\r", t0 + secs(10.9));
        for read in CLAUDE_CLASSIC_SIGNAL_READS {
            model.feed_at(read, t0 + secs(11.0));
        }
        let ended = ["> question", "the last answer", "", ">", "a quick reply", ">"].map(String::from).to_vec();
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

    /// Prod 2026-10-08 (desktop 0.4.680): the kept picture of an ended session still said
    /// "● Live · Isol8 Admin watching [ Pause ] ctrl+x tab, then p".
    #[test]
    fn holder_model_kept_picture_has_no_lightbulb_band() {
        let t0 = Instant::now();
        let kept_at = |cols: u16, screen: &str, alternate: bool| {
            let mut model = Model::new(12, cols);
            if alternate {
                model.feed_at(b"\x1b[?1049h\x1b[2J\x1b[H", t0);
            } else {
                model.classic_claude_code();
            }
            model.feed_at(screen.as_bytes(), t0);
            model.ending();
            let mut term = vt100::Parser::new(12, cols, 0);
            term.process(&model.end(t0 + QUIET).expect("a kept picture"));
            (lines(&mut term), term.screen().cursor_position())
        };
        let kept = |screen: &str, alternate: bool| kept_at(90, screen, alternate);
        let rows = |rows: &[&str]| rows.iter().map(|row| row.to_string()).collect::<Vec<_>>();
        const BAND: &str = "● Live · Ada watching [ Pause ] ctrl+x tab, then p            [-]";
        const ASKED: &str = "● Live · Ada watching [ Pause ]                               [-]";
        const ASK: &str = "Bo Li asked to join (+2 more) [ Allow ] [ Decline ] ctrl+x tab, then a or d · or click";
        for alternate in [false, true] {
            // The rows under the band move up, and the cursor with them.
            let screen = format!("the answer\r\n\r\n{BAND}\r\n\r\n> ");
            assert_eq!(kept(screen.as_str(), alternate), (rows(&["the answer", "", "", ">"]), (3, 2)), "alternate screen: {alternate}");
            // A join request's row and the approval line go with it; a band the conversation quotes stays.
            let screen = format!("{BAND}\r\nsaid the docs\r\n○ Sharing paused [ Resume sharing ]\r\n{ASK}\r\nWaiting on you here or in Lightbulb\r\n\r\n> ");
            assert_eq!(kept(screen.as_str(), alternate).0, rows(&[BAND, "said the docs", "", ">"]));
            let screen = format!("{ASKED}\r\nAda wants to pick this up · conversation + uncommitted changes [ Allow ] [ Decline ] ctrl+x tab, then a or d · or click\r\n> ");
            assert_eq!(kept(screen.as_str(), alternate).0, rows(&[">"]));
            // No band: the picture as it is.
            assert_eq!(kept("the answer\r\n\r\n> ", alternate), (rows(&["the answer", "", ">"]), (2, 2)));
        }
        // Every band the mod draws today goes the same way.
        for band in ["● Live in Lightbulb [ Stop sharing ] ctrl+x tab, then p", "● Live · Ada watching [ Stop sharing ]", "● Shared in Lightbulb [ Stop sharing ]",
            "● Live · 3 via link [ Stop sharing ] ctrl+x tab, then p", "● Live · 2 watching · 1 typing · 3 via link [ Stop sharing ] ctrl+x tab, then p",
            "○ Only you [ Share to Lightbulb ] ctrl+x tab, then s", "○ Stopping… teammates may still see earlier output", "○ Press ← once to go live"] {
            let screen = format!("the answer\r\n\r\n{band}\r\n\r\n> ");
            assert_eq!(kept(screen.as_str(), false).0, rows(&["the answer", "", "", ">"]), "{band}");
        }
        // Review of #3878: with no request, what stands under the band is the person's own, whatever it says.
        for under in ["> press [ Allow ] to go on", "> Bo asked to join [ Allow ] [ Decline ]", "Waiting on you here", "> ctrl+x tab, then p"] {
            let screen = format!("{BAND}\r\n{under}\r\nmore");
            assert_eq!(kept(screen.as_str(), false).0, rows(&[under, "more"]), "{under}");
        }
        // The same words with no band over them, or a band's opening with no button after it: not a band.
        for screen in [format!("the answer\r\n{ASK}\r\nWaiting on you here or in Lightbulb"), "● Live · said the docs\r\n\r\n> [ Pause ]".to_owned(), "● Lively [ Pause ]".to_owned()] {
            let shown: Vec<String> = screen.split("\r\n").map(String::from).collect();
            assert_eq!(kept(screen.as_str(), false).0, shown);
        }
        // Review of #3878: at 40 columns every band wraps (the shortest is 44), and long names wrap any.
        let wrapped = "● Live in Lightbulb [ Pause ]        [-]\r\nctrl+x tab, then p\r\n\r\n> ";
        assert_eq!(kept_at(40, wrapped, false), (rows(&["", ">"]), (1, 2)));
        let names = "the answer\r\n● Live · Bartholomew Featherstonehaugh,\r\nMaximiliana Wolfeschlegelsteinhausen\r\nwatching · Bartholomew Featherstonehaugh\r\ntyping [ Pause ] ctrl+x tab,\r\nthen p\r\n\r\n> ";
        assert_eq!(kept_at(40, names, false).0, rows(&["the answer", "", ">"]));
        // Review of #3878: nothing caps the viewers the mod names, so the button can be any number of rows down.
        let many = format!("the answer\r\n● Live · {}Zed watching\r\n[ Pause ] ctrl+x tab, then p\r\n\r\n> ", "Bartholomew Featherstonehaugh,\r\n".repeat(7));
        assert_eq!(kept_at(40, many.as_str(), false).0, rows(&["the answer", "", ">"]));
        // The count of link viewers wraps in the text's column like the names do.
        let by_link = "the answer\r\n● Live · 2 watching  [ Pause ] ctrl+x tab,          [-]\r\n· 3 via link                    then p\r\n\r\n> ";
        assert_eq!(kept_at(60, by_link, false).0, rows(&["the answer", "", ">"]));
        let asked = "● Sharing is not a lead\r\n○ Sharing paused [ Resume\r\nsharing ]\r\nBartholomew Featherstonehaugh asked to\r\njoin [ Allow ] [ Decline ] ctrl+x tab,\r\nthen a or d · or click\r\n> ";
        assert_eq!(kept_at(40, asked, false).0, rows(&["● Sharing is not a lead", ">"]));
        // Prod 2026-10-08 (desktop 0.4.687, 60 columns): the request's parts wrap each in its own column, and
        // its two rows stayed in the kept picture. These two rows are the ones captured there.
        const LIVE: &str = "● Live · Isol8 Admin watching [ Pause ]                 [-]";
        let seen = format!("the answer\r\n\r\n{LIVE}\r\nIsol8 Admin    [ Allow ][ Decline ] ctrl+x tab, then a\r\nasked to join                      or d · or click\r\n────\r\n> ");
        assert_eq!(kept_at(60, seen.as_str(), false), (rows(&["the answer", "", "────", ">"]), (3, 2)));
        // The layouts from here on are worked out from that one (text and hint wrap by words in their columns, the
        // buttons keep their width), not captured. A pick-up request; the text is the tallest column.
        let pickup = format!("{LIVE}\r\nAda wants to pick this  [ Allow ][ Decline ] ctrl+x tab,\r\nup · conversation +                          then a or d ·\r\nuncommitted changes                          or click\r\nWaiting on you here or in Lightbulb\r\n> ");
        assert_eq!(kept_at(60, pickup.as_str(), false).0, rows(&[">"]));
        let more = format!("{LIVE}\r\nBo Li asked  [ Allow ][ Decline ] ctrl+x tab, then a or d\r\nto join (+2                       · or click\r\nmore)\r\n> ");
        assert_eq!(kept_at(60, more.as_str(), false).0, rows(&[">"]));
        // The band itself, its hint wrapped under the hint and its names under the names.
        let band = "the answer\r\n● Live · Isol8 Admin  [ Pause ] ctrl+x tab,          [-]\r\nwatching · Isol8                then p\r\nAdmin typing\r\n\r\n> ";
        assert_eq!(kept_at(60, band, false).0, rows(&["the answer", "", ">"]));
        // The person's own rows under such a block stay, whatever they say; so does a block with a part missing or out of its column.
        for under in ["> Bo asked to join", "> (+2 more)", "> or d · or click"] {
            let screen = format!("{LIVE}\r\nIsol8 Admin    [ Allow ][ Decline ] ctrl+x tab, then a\r\nasked to join                      or d · or click\r\n{under}\r\nmore");
            assert_eq!(kept_at(60, screen.as_str(), false).0, rows(&[under, "more"]), "{under}");
        }
        for under in ["Isol8 Admin    [ Allow ][ Decline ] ctrl+x tab, then a\r\nasked to join", "Isol8 Admin    [ Allow ] ctrl+x tab, then a\r\nasked to join  [ Decline ]         or d · or click", "> I was watching"] {
            let screen = format!("{LIVE}\r\n{under}");
            let shown: Vec<String> = under.split("\r\n").map(String::from).collect();
            assert_eq!(kept_at(60, screen.as_str(), false).0, shown, "{under}");
        }
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

    /// The screen rows and the cursor: what a terminal shows, whatever is above it.
    fn shown(screen: &vt100::Screen) -> (Vec<String>, (u16, u16)) {
        (screen.rows(0, screen.size().1).collect(), screen.cursor_position())
    }

    /// A new viewer's terminal: the model's history, then its snapshot as the web view draws it.
    fn late_joiner(model: &mut Model, max: usize) -> vt100::Parser {
        let (rows, cols) = model.size();
        let mut term = vt100::Parser::new(rows, cols, SCROLLBACK);
        term.process(&model.history(max));
        term.process(WEB_CLEAR);
        term.process(&model.snapshot());
        term
    }

    /// Codex's inline mode (`run::CODEX_INLINE`) writes each finished line through a scroll region
    /// from the first row to the row above its prompt. A terminal keeps the lines that leave that
    /// region's top; vt100 0.16 alone keeps none of them, so the model had no history for Codex.
    #[test]
    fn holder_model_keeps_the_lines_that_leave_the_top_of_a_scroll_region_from_the_first_row() {
        // Codex's `insert_history`: the region, its last row, CR LF and the line, the region off, the prompt.
        let insert = |line: &str| format!("\x1b[1;4r\x1b[4;1H\r\n\x1b[1m{line}\x1b[m\x1b[r\x1b[5;9H").into_bytes();
        let mut output = b"\x1b[5;1H> prompt\x1b[6;1Hstatus".to_vec();
        for i in 1..=9 {
            output.extend(insert(&format!("line {i}")));
        }
        let expected: Vec<String> = ["", "", "", ""].into_iter().map(str::to_owned)
            .chain((1..=9).map(|i| format!("line {i}"))).chain(["> prompt".to_owned(), "status".to_owned()]).collect();
        // Whole, and a byte a read: a sequence split anywhere is still followed.
        for split in [false, true] {
            let mut model = Model::new(6, 20);
            let mut plain = vt100::Parser::new(6, 20, SCROLLBACK);
            plain.process(&output);
            if split {
                output.iter().for_each(|b| { model.feed(&[*b]); });
            } else {
                model.feed(&output);
            }
            assert_eq!(above(&mut plain), 0, "vt100 keeps no line while a region is set");
            assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "the screen and the cursor are the ones vt100 draws");
            // The four blank rows above the first line left the region first, as in a terminal.
            assert_eq!(lines(&mut late_joiner(&mut model, 1 << 20)), expected, "split: {split}");
            assert_eq!(above(&mut late_joiner(&mut model, 1 << 20)), 9);
            // The region is still the provider's: one more line scrolls it again.
            model.feed(&insert("line 10"));
            assert_eq!(lines(&mut late_joiner(&mut model, 1 << 20)).last_chunk::<3>().unwrap(), &["line 10".to_owned(), "> prompt".to_owned(), "status".to_owned()]);
        }
    }

    #[test]
    fn holder_model_keeps_the_line_a_wrapping_character_pushes_off_a_scroll_region_and_no_line_of_other_regions() {
        // Whole reads, and a byte a read: a pty read can end inside a character (review of #3912: a
        // wide character on the region's last column, split across two reads, was taken as one
        // column wide, vt100 wrapped it on the next read, and the line it pushed off was not kept).
        for split in [false, true] {
            let mut model = Model::new(4, 10);
            let mut plain = vt100::Parser::new(4, 10, SCROLLBACK);
            let feed = |model: &mut Model, plain: &mut vt100::Parser, bytes: &[u8]| {
                for read in bytes.chunks(if split { 1 } else { bytes.len() }) {
                    model.feed(read);
                    plain.process(read);
                }
            };
            // Region rows 1-3, a prompt under it. A line longer than the terminal is wide wraps on the
            // region's last row, twice; a wide character with one column left wraps too (3 bytes
            // of CJK, 4 of an emoji).
            feed(&mut model, &mut plain, "\x1b[4;1Hprompt\x1b[1;3r\x1b[3;1H\r\naaaaaaaaaabbbbbbbbbbcc\r\nddddddddd中\r\neeeeeeeee😀\x1b[r".as_bytes());
            assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "split: {split}");
            let all = lines(&mut late_joiner(&mut model, 1 << 20));
            assert_eq!(all, ["", "", "", "aaaaaaaaaa", "bbbbbbbbbb", "cc", "ddddddddd", "中", "eeeeeeeee", "😀", "prompt"], "split: {split}");
            let kept = above(&mut late_joiner(&mut model, 1 << 20));
            assert_eq!(kept, 7);
            // A narrow character of 2 bytes on the last column does not wrap, however it is read.
            feed(&mut model, &mut plain, "\x1b[1;3r\x1b[3;1H\x1b[2Kfffffffffé\x1b[r".as_bytes());
            assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "split: {split}");
            assert_eq!(above(&mut late_joiner(&mut model, 1 << 20)), kept);
            // A region that does not start at the first row keeps nothing in a terminal either, nor does
            // the alternate screen, nor `ESC[<n>S`.
            feed(&mut model, &mut plain, b"\x1b[2;3r\x1b[3;1H\r\nlower\r\nregion\x1b[r");
            feed(&mut model, &mut plain, b"\x1b[1;3r\x1b[2S\x1b[r");
            feed(&mut model, &mut plain, b"\x1b[?1049h\x1b[1;3r\x1b[3;1H\r\nfull\r\nscreen\x1b[r\x1b[?1049l");
            assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "split: {split}");
            assert_eq!(above(&mut late_joiner(&mut model, 1 << 20)), kept);
            // No region: vt100 keeps the line itself, once.
            feed(&mut model, &mut plain, b"\x1b[4;1H\r\nplain");
            assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "split: {split}");
            assert_eq!(above(&mut late_joiner(&mut model, 1 << 20)), kept + 1);
        }
    }

    /// Prod 2026-10-08 (acceptance round 14, C3): a teammate who opened a long inline Codex session
    /// got its output log's tail. At 218 KB the first 28 of 1313 rows were wrong (a line out of
    /// place, then 24 blank rows: the empty rows of a new terminal scrolling off as the tail
    /// replayed), and the head of the session was missing. This is that session's real output (Codex
    /// 0.159.3, 100x30, each read as it arrived, bytes as Latin-1): start, one answer, six `/status`
    /// blocks. `lines` is every line a real terminal (headless xterm 5.5) then holds, `above` how
    /// many of them are above its screen.
    #[test]
    fn holder_model_gives_a_late_joiner_of_real_inline_codex_every_line_a_terminal_kept() {
        let capture: serde_json::Value = serde_json::from_str(include_str!("../../tests/fixtures/codex_inline_status.json")).unwrap();
        let number = |key: &str| capture[key].as_u64().unwrap();
        let (rows, cols) = (number("rows") as u16, number("cols") as u16);
        let kept: Vec<String> = capture["lines"].as_array().unwrap().iter().map(|l| l.as_str().unwrap().to_owned()).collect();
        let mut model = Model::new(rows, cols);
        model.inline_codex();
        let mut plain = vt100::Parser::new(rows, cols, SCROLLBACK);
        for read in capture["reads"].as_array().unwrap() {
            let bytes: Vec<u8> = read.as_str().unwrap().chars().map(|c| c as u8).collect();
            model.feed(&bytes);
            plain.process(&bytes);
        }
        assert_eq!(shown(model.parser.screen()), shown(plain.screen()), "the screen and the cursor are the ones vt100 draws");
        let mut late = late_joiner(&mut model, 1 << 20);
        assert_eq!(lines(&mut late), kept, "every line, each once, none blank or out of place");
        assert_eq!(above(&mut late) as u64, number("above"));
        assert!(above(&mut plain) < above(&mut late), "vt100 alone keeps fewer: {}", above(&mut plain));
        // A short budget: the newest whole lines, and nothing before them.
        let mut short = late_joiner(&mut model, 4096);
        let newest = lines(&mut short);
        assert!(newest.len() > usize::from(rows) && newest.len() < kept.len() && kept.ends_with(&newest), "{newest:#?}");
        // Ended (Codex leaves its last screen): the same lines, and nobody starts over.
        assert_eq!(model.end(Instant::now()), None);
        assert_eq!(model.starts_over(), None);
        assert_eq!(lines(&mut late_joiner(&mut model, 1 << 20)), kept);
    }

    #[test]
    fn scrolled_returns_the_rows_that_left_the_top_oldest_first() {
        let mut m = Model::new(3, 10);
        let _ = m.feed(b"a\r\nb\r\nc");
        assert_eq!(m.scrolled(), (false, vec![]));
        let _ = m.feed(b"\r\nd\r\ne");
        let (erased, rows) = m.scrolled();
        assert!(!erased);
        assert_eq!(rows.iter().map(|r| String::from_utf8_lossy(r).trim_end().to_string()).collect::<Vec<_>>(), vec!["a", "b"]);
        assert_eq!(m.scrolled(), (false, vec![]));
    }

    #[test]
    fn scrolled_keeps_a_codex_region_row_and_reports_an_erase() {
        let mut m = Model::new(3, 10);
        m.inline_codex();
        let _ = m.feed(b"a\r\nb\r\nc\x1b[1;2r\x1b[2;1H\r\nX\x1b[r");
        let (_, rows) = m.scrolled();
        assert_eq!(String::from_utf8_lossy(&rows[0]).trim_end(), "a");
        let _ = m.feed(b"\x1b[3J");
        assert!(m.scrolled().0);
    }

    #[test]
    fn scrolled_counts_past_the_scrollback_cap() {
        // Twice the cap, 2000 lines between calls: every line that left the screen, in order.
        let mut m = Model::new(3, 10);
        let mut got = Vec::new();
        for round in 0..5 {
            for i in round * 2000..(round + 1) * 2000 {
                let _ = m.feed(format!("{i}\r\n").as_bytes());
            }
            got.extend(m.scrolled().1.iter().map(|r| String::from_utf8_lossy(r).trim_end().to_string()));
        }
        // (The screen keeps the last two lines and the blank row under them.)
        assert!(got == (0..9998).map(|i| i.to_string()).collect::<Vec<_>>(), "{} rows, first {:?}", got.len(), got.first());
    }

    /// Codex review of #4035: with Codex's region set the whole time, the count stopped at the cap
    /// and no later row reached a viewer.
    #[test]
    fn scrolled_counts_past_the_cap_while_a_codex_region_stays_set() {
        let mut m = Model::new(6, 10);
        m.inline_codex();
        let _ = m.feed(b"\x1b[1;4r\x1b[4;1H");
        let mut got = Vec::new();
        for round in 0..4 {
            for i in round * 1600..(round + 1) * 1600 {
                let _ = m.feed(format!("\r\n{i}").as_bytes());
            }
            got.extend(m.scrolled().1.iter().map(|r| String::from_utf8_lossy(r).trim_end().to_string()));
        }
        // (The region's four blank rows leave first; it keeps the last four lines.)
        let want: Vec<String> = std::iter::repeat_n(String::new(), 4).chain((0..6396).map(|i| i.to_string())).collect();
        assert!(got == want, "{} rows, last {:?}", got.len(), got.last());
        assert_eq!(m.screen().contents(), "6396\n6397\n6398\n6399", "the region is still set: the rows under it stay blank");
    }

    #[test]
    fn scrolled_waits_for_the_end_of_a_sequence_or_a_character_to_start_over() {
        for (first, rest) in [(&b"\x1b[3"[..], &b"1mX"[..]), (b"\xc3", b"\xa9X")] {
            let mut m = Model::new(3, 10);
            for i in 0..2600 {
                let _ = m.feed(format!("{i}\r\n").as_bytes());
            }
            let _ = m.feed(first);
            let _ = m.scrolled();
            assert!(m.taken > 0, "no new parser inside {first:?}");
            let _ = m.feed(rest);
            let shown = m.screen().contents();
            assert!(shown.ends_with("\nX") || shown.ends_with("\néX"), "{shown:?}");
            let _ = m.scrolled();
            assert_eq!(m.taken, 0, "a new parser once it ended");
        }
    }
}
