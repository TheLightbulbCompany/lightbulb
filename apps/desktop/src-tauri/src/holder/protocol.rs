//! Holder wire frames: `[u8 kind][u32 BE length][payload]`, both directions on one socket.

use std::io::{Error, ErrorKind, Read, Result};

/// Client → holder: `{"role":"interactive"|"watch"|"type"|"recorder"|"origin", "cols"?, "rows"?, "replayFrom"?, "join"?}`.
/// Holder → client: session facts (`"watch": true` when it serves watch clients, `"type": true`
/// when it takes `type` ones, `"joined": true` when a `join` client found an interactive one,
/// `"origin": true` when it takes OWNER_INPUT and reports `"lastInput"`: who wrote the last bytes
/// to the pty, `"owner"`, `"teammate"` (a `type` client's plain INPUT) or null). An `origin`
/// client gets the facts and nothing else: it is how the app asks for `lastInput`.
/// An interactive `join` client never takes the keyboard from another: while one is attached it
/// watches and types at that client's size (its RESIZE is ignored), and gets PROMOTED when that
/// client leaves. Without one it is the interactive client.
pub const HELLO: u8 = 1;
/// Holder → client: `u64 BE offset` of the first byte, then provider output.
pub const OUTPUT: u8 = 2;
/// Interactive or `type` client → holder: bytes for the pty.
pub const INPUT: u8 = 3;
/// Interactive client → holder (a `type` client's is ignored), and holder → watch clients after a size change:
/// `u16 BE cols`, `u16 BE rows`.
pub const RESIZE: u8 = 4;
/// Holder → client: the provider exited, `{"status":"…","code":N}`.
pub const EXIT: u8 = 5;
/// Client → holder: `u64 BE from`; answered with OUTPUT from there (and GAP first if it is gone).
pub const REPLAY: u8 = 6;
/// Holder → client: `u64 BE from`, `u64 BE to`; output in that range is no longer held.
pub const GAP: u8 = 7;
/// Holder → interactive and watch clients on attach, and → a watch client that sends an empty
/// SNAPSHOT: `u64 BE offset`, then the screen and its modes.
pub const SNAPSHOT: u8 = 8;
/// Holder → the interactive client another client replaced.
pub const DETACHED: u8 = 9;
/// Holder → a joined client: the interactive client left and this one is it now (empty payload).
/// It sends its own RESIZE next.
pub const PROMOTED: u8 = 10;
/// `type` client → holder: INPUT that the session's owner typed (in the Lightbulb app). Sent only
/// to a holder whose HELLO says `"origin": true`; an older holder ignores this kind.
pub const OWNER_INPUT: u8 = 11;

/// No frame is larger than this; a longer declared length is refused before allocating it.
pub const MAX_PAYLOAD: usize = 8 << 20;

pub fn encode(kind: u8, payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(5 + payload.len());
    frame.push(kind);
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(payload);
    frame
}

pub fn read_frame(r: &mut impl Read) -> Result<(u8, Vec<u8>)> {
    let mut head = [0u8; 5];
    r.read_exact(&mut head)?;
    let len = u32::from_be_bytes([head[1], head[2], head[3], head[4]]) as usize;
    if len > MAX_PAYLOAD {
        return Err(Error::new(ErrorKind::InvalidData, "holder frame too large"));
    }
    let mut payload = vec![0u8; len];
    r.read_exact(&mut payload)?;
    Ok((head[0], payload))
}

/// An offset-prefixed payload (OUTPUT, SNAPSHOT, REPLAY): the offset and the rest.
pub fn split_offset(payload: &[u8]) -> Option<(u64, &[u8])> {
    let head: [u8; 8] = payload.get(..8)?.try_into().ok()?;
    Some((u64::from_be_bytes(head), &payload[8..]))
}

pub fn with_offset(offset: u64, bytes: &[u8]) -> Vec<u8> {
    let mut payload = offset.to_be_bytes().to_vec();
    payload.extend_from_slice(bytes);
    payload
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn holder_frames_round_trip_every_kind() {
        for kind in [HELLO, OUTPUT, INPUT, RESIZE, EXIT, REPLAY, GAP, SNAPSHOT, DETACHED, PROMOTED, OWNER_INPUT] {
            let bytes = encode(kind, b"payload\x00\xff");
            assert_eq!(read_frame(&mut bytes.as_slice()).unwrap(), (kind, b"payload\x00\xff".to_vec()));
        }
        let empty = encode(DETACHED, b"");
        assert_eq!(read_frame(&mut empty.as_slice()).unwrap(), (DETACHED, vec![]));
    }

    #[test]
    fn holder_frames_refuse_an_oversized_length_without_allocating_it() {
        let mut bytes = vec![OUTPUT];
        bytes.extend_from_slice(&u32::MAX.to_be_bytes());
        assert_eq!(read_frame(&mut bytes.as_slice()).unwrap_err().kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn holder_offsets_round_trip() {
        let payload = with_offset(1 << 40, b"abc");
        assert_eq!(split_offset(&payload), Some((1 << 40, &b"abc"[..])));
        assert_eq!(split_offset(b"short"), None);
    }
}
