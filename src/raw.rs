//! Raw / JetDirect (port 9100): job capture plus PJL, ESC/POS and ZPL status replies.

use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::thread;
use std::time::Duration;

use crate::printer::{Printer, State};

const UEL: &[u8] = b"\x1b%-12345X";

// --------------------------------------------------------------------------
// Query scanner. Hand-written equivalent of the Python QUERY_RE:
//   (\x1b%-12345X) | @PJL (INFO \w+ | USTATUS \w+=\w+ | ECHO[^\r\n]* | INQUIRE \w+ | DINQUIRE \w+)\r?\n?
//   | \x10\x04. | \x10\x05. | ~HS | ~HI | ~HQES | ~HQ\w+
// --------------------------------------------------------------------------
#[derive(Debug, PartialEq)]
enum Tok {
    Uel,
    Pjl(String),
    Escpos(u8),
    Dle05,
    Hs,
    Hi,
    Hqes,
    OtherHq,
}

fn word_len(b: &[u8]) -> usize {
    b.iter().take_while(|c| c.is_ascii_alphanumeric() || **c == b'_').count()
}

/// Length of the PJL command text after "@PJL " (None if no alternative matches).
fn pjl_cmd_len(rest: &[u8]) -> Option<usize> {
    if let Some(x) = rest.strip_prefix(b"INFO ") {
        let n = word_len(x);
        if n > 0 {
            return Some(5 + n);
        }
    }
    if let Some(x) = rest.strip_prefix(b"USTATUS ") {
        let n = word_len(x);
        if n > 0 && x.get(n) == Some(&b'=') {
            let m = word_len(&x[n + 1..]);
            if m > 0 {
                return Some(8 + n + 1 + m);
            }
        }
    }
    if let Some(x) = rest.strip_prefix(b"ECHO") {
        let n = x.iter().take_while(|c| **c != b'\r' && **c != b'\n').count();
        return Some(4 + n);
    }
    if let Some(x) = rest.strip_prefix(b"INQUIRE ") {
        let n = word_len(x);
        if n > 0 {
            return Some(8 + n);
        }
    }
    if let Some(x) = rest.strip_prefix(b"DINQUIRE ") {
        let n = word_len(x);
        if n > 0 {
            return Some(9 + n);
        }
    }
    None
}

fn match_at(b: &[u8], i: usize) -> Option<(usize, Tok)> {
    let r = &b[i..];
    if r.starts_with(UEL) {
        return Some((i + UEL.len(), Tok::Uel));
    }
    if let Some(rest) = r.strip_prefix(b"@PJL ") {
        let n = pjl_cmd_len(rest)?;
        let cmd = String::from_utf8_lossy(&rest[..n]).into_owned();
        let mut end = i + 5 + n;
        if b.get(end) == Some(&b'\r') {
            end += 1;
        }
        if b.get(end) == Some(&b'\n') {
            end += 1;
        }
        return Some((end, Tok::Pjl(cmd)));
    }
    if r.len() >= 3 && r[0] == 0x10 && r[1] == 0x04 {
        return Some((i + 3, Tok::Escpos(r[2])));
    }
    if r.len() >= 3 && r[0] == 0x10 && r[1] == 0x05 {
        return Some((i + 3, Tok::Dle05));
    }
    if r.starts_with(b"~HS") {
        return Some((i + 3, Tok::Hs));
    }
    if r.starts_with(b"~HI") {
        return Some((i + 3, Tok::Hi));
    }
    if r.starts_with(b"~HQES") {
        return Some((i + 5, Tok::Hqes));
    }
    if let Some(x) = r.strip_prefix(b"~HQ") {
        let n = word_len(x);
        if n > 0 {
            return Some((i + 3 + n, Tok::OtherHq));
        }
    }
    None
}

/// All non-overlapping query tokens, left to right: (start, end, token).
fn scan(buf: &[u8]) -> Vec<(usize, usize, Tok)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < buf.len() {
        match match_at(buf, i) {
            Some((end, tok)) => {
                out.push((i, end, tok));
                i = end;
            }
            None => i += 1,
        }
    }
    out
}

/// True if anything other than query tokens and whitespace/NULs is present (i.e. it is a real job).
fn has_job_data(buf: &[u8]) -> bool {
    let mut pos = 0;
    let mut rest: Vec<u8> = Vec::new();
    for (s, e, _) in scan(buf) {
        rest.extend_from_slice(&buf[pos..s]);
        pos = e;
    }
    rest.extend_from_slice(&buf[pos..]);
    rest.iter().any(|b| !matches!(b, b'\r' | b'\n' | 0x0c | b' ' | 0))
}

// --------------------------------------------------------------------------
// replies (representative codes: adjust to your target)
// --------------------------------------------------------------------------
fn pjl_reply(p: &Printer, cmd: &str) -> Vec<u8> {
    let (code, disp, online) = match p.state() {
        State::PaperOut => (30010, "Load Paper", "TRUE"),
        State::Jam => (30016, "Paper Jam", "TRUE"),
        State::Offline => (10023, "Offline", "FALSE"),
        _ => (10001, "Ready", "TRUE"),
    };
    if cmd.starts_with("INFO STATUS") || cmd.starts_with("USTATUS") {
        return format!("@PJL INFO STATUS\r\nCODE={}\r\nDISPLAY=\"{}\"\r\nONLINE={}\r\n\x0c", code, disp, online)
            .into_bytes();
    }
    if cmd.starts_with("INFO ID") {
        return format!("@PJL INFO ID\r\n\"{}\"\r\n\x0c", p.name).into_bytes();
    }
    if cmd.starts_with("INFO PAGECOUNT") {
        return b"@PJL INFO PAGECOUNT\r\n12345\r\n\x0c".to_vec();
    }
    if cmd.starts_with("INFO CONFIG") {
        return b"@PJL INFO CONFIG\r\nLANGUAGES=PCL PS PDF\r\nMEMORY=256MB\r\n\x0c".to_vec();
    }
    if cmd.starts_with("ECHO") {
        return format!("@PJL {}\r\n\x0c", cmd).into_bytes();
    }
    Vec::new()
}

fn escpos_reply(st: State, n: u8) -> Vec<u8> {
    let ok = 0x12;
    let b = match n {
        1 => if st == State::Offline { 0x1e } else { ok }, // printer status
        2 => if st == State::Jam { 0x52 } else { ok },      // offline cause
        3 => if st == State::Jam { 0x32 } else { ok },      // error cause
        4 => if st == State::PaperOut { 0x72 } else { ok }, // paper sensor
        _ => ok,
    };
    vec![b]
}

fn zpl_hs(st: State) -> Vec<u8> {
    let paper_out = (st == State::PaperOut) as u8;
    let pause = (st == State::Offline) as u8;
    let l1 = format!("030,{},{},1234,000,0,0,0,000,0,0,0", paper_out, pause);
    let l2 = "000,0,0,0,0,2,4,0,00000000,1,000";
    let l3 = "0000,0";
    [l1.as_str(), l2, l3].iter().map(|l| format!("\x02{}\x03\r\n", l)).collect::<String>().into_bytes()
}

/// Replies for the query tokens found in one received chunk (queries are matched per chunk).
fn raw_replies(p: &Printer, chunk: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    for (_, _, tok) in scan(chunk) {
        let r = match tok {
            Tok::Pjl(cmd) => pjl_reply(p, &cmd),
            Tok::Escpos(n) => escpos_reply(p.state(), n),
            Tok::Hs => zpl_hs(p.state()),
            Tok::Hi => format!("\x02{},V75.20.01Z,203,8176KB\x03\r\n", p.name).into_bytes(),
            Tok::Hqes => b"\x02\r\n  PRINTER STATUS\r\n  ERRORS: 0 00000000 00000000\r\n  WARNINGS: 0 00000000 00000000\r\n\x03\r\n".to_vec(),
            Tok::Uel | Tok::Dle05 | Tok::OtherHq => Vec::new(),
        };
        if !r.is_empty() {
            out.push(r);
        }
    }
    out
}

// --------------------------------------------------------------------------
pub fn handle(p: &Printer, mut stream: TcpStream) {
    match p.state() {
        State::Drop => return,
        State::Hang => {
            thread::sleep(Duration::from_secs(60));
            return;
        }
        _ => {}
    }
    let peer = stream.peer_addr().map(|a| a.ip().to_string()).unwrap_or_default();
    let _ = stream.set_read_timeout(Some(p.idle));
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = vec![0u8; 65536];
    loop {
        let n = match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break, // idle timeout or connection error
        };
        buf.extend_from_slice(&chunk[..n]);
        let mut failed = false;
        for r in raw_replies(p, &chunk[..n]) {
            if stream.write_all(&r).is_err() {
                failed = true;
                break;
            }
        }
        if failed {
            break;
        }
    }
    // query-only sessions are not jobs
    if has_job_data(&buf) {
        if let Err(e) = p.save_job("raw", &buf) {
            eprintln!("[raw] cannot save job: {}", e);
        }
    } else {
        println!("[raw] status query from {}", peer);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(b: &[u8]) -> Vec<Tok> {
        scan(b).into_iter().map(|(_, _, t)| t).collect()
    }

    #[test]
    fn scans_pjl_and_friends() {
        assert_eq!(
            toks(b"\x1b%-12345X@PJL INFO STATUS\r\n@PJL INFO ID\n"),
            vec![Tok::Uel, Tok::Pjl("INFO STATUS".into()), Tok::Pjl("INFO ID".into())]
        );
        assert_eq!(toks(b"@PJL USTATUS DEVICE=ON\r\n"), vec![Tok::Pjl("USTATUS DEVICE=ON".into())]);
        assert_eq!(toks(b"@PJL ECHO hello world\r\n"), vec![Tok::Pjl("ECHO hello world".into())]);
        assert_eq!(toks(b"\x10\x04\x01"), vec![Tok::Escpos(1)]);
        assert_eq!(toks(b"~HS~HI~HQES~HQXX"), vec![Tok::Hs, Tok::Hi, Tok::Hqes, Tok::OtherHq]);
        assert!(toks(b"@PJL INFO ").is_empty());
        assert!(toks(b"@PJL SET COPIES=2\r\n").is_empty());
    }

    #[test]
    fn job_detection() {
        assert!(!has_job_data(b"\x1b%-12345X@PJL INFO STATUS\r\n\x1b%-12345X"));
        assert!(!has_job_data(b"~HS\r\n"));
        assert!(has_job_data(b"hello\n"));
        assert!(has_job_data(b"@PJL INFO ID\r\n^XA^XZ"));
    }
}
