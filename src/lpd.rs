//! LPD / LPR (RFC 1179).

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::thread;
use std::time::Duration;

use crate::printer::{Printer, State};

const MAX_LINE: u64 = 8192;

fn read_line<R: BufRead>(r: &mut R) -> io::Result<Vec<u8>> {
    let mut buf = Vec::new();
    r.by_ref().take(MAX_LINE).read_until(b'\n', &mut buf)?;
    Ok(buf)
}

fn ack<W: Write>(w: &mut W) -> io::Result<()> {
    w.write_all(b"\0")?;
    w.flush()
}

/// Byte count from a subcommand header such as `\x03123 dfA001host\n`.
fn parse_count(rest: &[u8]) -> io::Result<u64> {
    let first = rest.split(|&b| b == b' ').next().unwrap_or(&[]);
    std::str::from_utf8(first)
        .ok()
        .and_then(|s| s.trim().parse::<u64>().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad LPD subcommand length"))
}

pub fn handle(p: &Printer, stream: TcpStream) {
    match p.state() {
        State::Drop => return,
        State::Hang => {
            thread::sleep(Duration::from_secs(60));
            return;
        }
        _ => {}
    }
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let mut w = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut r = BufReader::new(stream);
    if let Err(e) = session(p, &mut r, &mut w) {
        eprintln!("[lpd] {}", e);
    }
}

fn session<R: BufRead, W: Write>(p: &Printer, r: &mut R, w: &mut W) -> io::Result<()> {
    let line = read_line(r)?;
    if line.is_empty() {
        return Ok(());
    }
    let cmd = line[0];
    let queue = String::from_utf8_lossy(&line[1..]).trim().to_string();
    if cmd == 3 || cmd == 4 {
        // short / long queue state
        return w.write_all(format!("{} is ready\nno entries\n", queue).as_bytes());
    }
    if cmd != 2 {
        return Ok(()); // 1 = print waiting jobs; anything else ignored
    }
    ack(w)?;
    let mut data: Vec<u8> = Vec::new();
    loop {
        let hdr = read_line(r)?;
        if hdr.is_empty() || hdr[0] == 1 {
            break;
        }
        if hdr[0] == 2 || hdr[0] == 3 {
            let count = parse_count(&hdr[1..])?;
            ack(w)?;
            let mut blob = Vec::new();
            r.by_ref().take(count).read_to_end(&mut blob)?;
            let mut trailer = [0u8; 1];
            let _ = r.read(&mut trailer)?;
            ack(w)?;
            if hdr[0] == 3 {
                data.extend_from_slice(&blob);
            }
        } else {
            break;
        }
    }
    if !data.is_empty() {
        p.save_job("lpd", &data)?;
    }
    Ok(())
}
