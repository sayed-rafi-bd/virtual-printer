//! Minimal HTTP/1.1 server used for the IPP port and the web port:
//!   POST (Content-Type: application/ipp)  -> IPP
//!   GET /                                 -> dashboard (web/index.html, embedded in the binary)
//!   GET /api/status                       -> JSON status + recent jobs
//!   GET /set?state=jam                    -> change printer state
//!   GET /jobs/<file>                      -> download a received job

use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::thread;
use std::time::Duration;

use crate::ipp;
use crate::printer::{http_date, Printer, State};

const DASHBOARD: &str = include_str!("../web/index.html");
const MAX_LINE: u64 = 65536;
const MAX_HEADERS: usize = 100;

enum ReqErr {
    Closed,
    Bad(&'static str),
}

struct Request {
    method: String,
    target: String,
    http11: bool,
    headers: Vec<(String, String)>,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

fn read_line<R: BufRead>(r: &mut R) -> Result<Vec<u8>, ReqErr> {
    let mut buf = Vec::new();
    let n = r.by_ref().take(MAX_LINE).read_until(b'\n', &mut buf).map_err(|_| ReqErr::Closed)?;
    if n == 0 {
        return Err(ReqErr::Closed);
    }
    if !buf.ends_with(b"\n") && n as u64 >= MAX_LINE {
        return Err(ReqErr::Bad("line too long"));
    }
    Ok(buf)
}

fn read_request<R: BufRead>(r: &mut R) -> Result<Request, ReqErr> {
    let line = read_line(r)?;
    let line = String::from_utf8_lossy(&line).trim_end().to_string();
    let mut parts = line.split_whitespace();
    let (method, target, version) = match (parts.next(), parts.next(), parts.next()) {
        (Some(m), Some(t), Some(v)) => (m, t, v),
        _ => return Err(ReqErr::Bad("bad request line")),
    };
    if !version.starts_with("HTTP/") {
        return Err(ReqErr::Bad("bad HTTP version"));
    }
    let mut headers = Vec::new();
    loop {
        let h = read_line(r)?;
        let h = String::from_utf8_lossy(&h).trim_end().to_string();
        if h.is_empty() {
            break;
        }
        if headers.len() >= MAX_HEADERS {
            return Err(ReqErr::Bad("too many headers"));
        }
        if let Some((k, v)) = h.split_once(':') {
            headers.push((k.trim().to_ascii_lowercase(), v.trim().to_string()));
        }
    }
    Ok(Request {
        method: method.to_string(),
        target: target.to_string(),
        http11: version == "HTTP/1.1",
        headers,
    })
}

fn read_body<R: BufRead>(r: &mut R, req: &Request) -> Result<Vec<u8>, ReqErr> {
    let chunked = req.header("transfer-encoding").map(|v| v.eq_ignore_ascii_case("chunked")).unwrap_or(false);
    let mut body = Vec::new();
    if chunked {
        loop {
            let l = read_line(r)?;
            let l = String::from_utf8_lossy(&l).to_string();
            let size_str = l.split(';').next().unwrap_or("").trim();
            let size = if size_str.is_empty() { 0 } else { u64::from_str_radix(size_str, 16).map_err(|_| ReqErr::Bad("bad chunk size"))? };
            if size == 0 {
                // trailers until blank line
                loop {
                    let t = read_line(r)?;
                    if String::from_utf8_lossy(&t).trim().is_empty() {
                        break;
                    }
                }
                return Ok(body);
            }
            r.by_ref().take(size).read_to_end(&mut body).map_err(|_| ReqErr::Closed)?;
            read_line(r)?; // CRLF after the chunk
        }
    }
    let len: u64 = match req.header("content-length") {
        Some(v) => v.trim().parse().map_err(|_| ReqErr::Bad("bad Content-Length"))?,
        None => 0,
    };
    r.by_ref().take(len).read_to_end(&mut body).map_err(|_| ReqErr::Closed)?;
    Ok(body)
}

fn reason(code: u16) -> &'static str {
    match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        _ => "OK",
    }
}

fn send<W: Write>(w: &mut W, code: u16, ctype: &str, body: &[u8], close: bool) -> io::Result<()> {
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nServer: VirtualPrinter/1.0\r\nDate: {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n",
        code, reason(code), http_date(), ctype, body.len()
    );
    if close {
        head.push_str("Connection: close\r\n");
    }
    head.push_str("\r\n");
    let mut out = head.into_bytes();
    out.extend_from_slice(body);
    w.write_all(&out)?;
    w.flush()
}

fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

/// Everything the dashboard needs, as JSON (jobs: newest first, at most 100).
fn status_json(p: &Printer) -> String {
    let (count, bytes) = p.job_stats();
    let ports: Vec<String> = p
        .ports()
        .iter()
        .map(|(n, port)| format!("{{\"name\":{},\"port\":{}}}", json_str(n), port))
        .collect();
    let jobs: Vec<String> = p
        .recent_jobs(100)
        .iter()
        .rev()
        .map(|j| {
            format!(
                "{{\"id\":{},\"time\":{},\"proto\":{},\"file\":{},\"size\":{}}}",
                j.id, json_str(&j.time), json_str(j.proto), json_str(&j.file), j.size
            )
        })
        .collect();
    format!(
        "{{\"name\":{},\"state\":{},\"uptime\":{},\"ports\":[{}],\"jobs_count\":{},\"jobs_size\":{},\"jobs\":[{}]}}",
        json_str(&p.name), json_str(p.state().name()), json_str(&p.uptime_string()),
        ports.join(","), count, bytes, jobs.join(",")
    )
}

fn pct_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() && b[i + 1].is_ascii_hexdigit() && b[i + 2].is_ascii_hexdigit() => {
                out.push(u8::from_str_radix(&s[i + 1..i + 3], 16).unwrap());
                i += 2;
            }
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn query_get(query: &str, key: &str) -> Option<String> {
    query.split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
        if pct_decode(k) == key { Some(pct_decode(v)) } else { None }
    })
}

pub fn handle(p: &Printer, stream: TcpStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(120)));
    let mut w = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut r = BufReader::new(stream);
    loop {
        let req = match read_request(&mut r) {
            Ok(q) => q,
            Err(ReqErr::Closed) => return,
            Err(ReqErr::Bad(msg)) => {
                let _ = send(&mut w, 400, "text/plain", msg.as_bytes(), true);
                return;
            }
        };
        let conn_close = req.header("connection").map(|v| v.to_ascii_lowercase().contains("close")).unwrap_or(false);
        let mut close = conn_close || !req.http11;

        // curl / CUPS clients may wait for this before sending the body
        if req.http11 && req.header("expect").map(|v| v.eq_ignore_ascii_case("100-continue")).unwrap_or(false) {
            if w.write_all(b"HTTP/1.1 100 Continue\r\n\r\n").is_err() {
                return;
            }
        }

        let path = req.target.split('?').next().unwrap_or("").to_string();
        let query = req.target.split_once('?').map(|(_, q)| q.to_string()).unwrap_or_default();

        let result = match req.method.as_str() {
            "POST" => {
                match p.state() {
                    State::Drop => return,
                    State::Hang => {
                        thread::sleep(Duration::from_secs(30));
                        return;
                    }
                    _ => {}
                }
                let body = match read_body(&mut r, &req) {
                    Ok(b) => b,
                    Err(ReqErr::Closed) => return,
                    Err(ReqErr::Bad(msg)) => {
                        let _ = send(&mut w, 400, "text/plain", msg.as_bytes(), true);
                        return;
                    }
                };
                do_post(p, &mut w, &req, &path, &body, close)
            }
            "GET" => do_get(p, &mut w, &path, &query, close),
            _ => {
                close = true; // unread body would desync a kept-alive connection
                send(&mut w, 501, "text/plain", b"unsupported method", true)
            }
        };
        if result.is_err() || close {
            return;
        }
    }
}

fn do_post<W: Write>(p: &Printer, w: &mut W, req: &Request, path: &str, body: &[u8], close: bool) -> io::Result<()> {
    let is_ipp = req.header("content-type").map(|v| v.contains("application/ipp")).unwrap_or(false);
    if !is_ipp {
        return send(w, 404, "text/plain", b"not found", close);
    }
    let host = req.header("host").unwrap_or("localhost");
    let uri = format!("ipp://{}{}", host, path);
    match ipp::handle(p, body, &uri) {
        Ok(resp) => send(w, 200, "application/ipp", &resp, close),
        Err(e) => {
            println!("[ipp] bad request: {}", e);
            send(w, 400, "text/plain", b"bad IPP request", close)
        }
    }
}

fn do_get<W: Write>(p: &Printer, w: &mut W, path: &str, query: &str, close: bool) -> io::Result<()> {
    // no hang/drop guard here: the control endpoint must stay usable to switch the state back
    if path == "/set" {
        let s = query_get(query, "state").unwrap_or_default();
        return match State::parse(&s) {
            Some(st) => {
                p.set_state(st);
                println!("[state] -> {}", st.name());
                send(w, 200, "text/plain", format!("state={}\n", st.name()).as_bytes(), close)
            }
            None => send(w, 400, "text/plain", format!("state must be one of ({})\n", State::ALL.iter().map(|s| format!("'{}'", s.name())).collect::<Vec<_>>().join(", ")).as_bytes(), close),
        };
    }
    if let Some(rest) = path.strip_prefix("/jobs/") {
        let name = rest.rsplit('/').next().unwrap_or("");
        let file = p.jobdir.join(name);
        if !name.is_empty() && Path::new(&file).is_file() {
            return match fs::read(&file) {
                Ok(data) => send(w, 200, "application/octet-stream", &data, close),
                Err(_) => send(w, 500, "text/plain", b"cannot read job", close),
            };
        }
        return send(w, 404, "text/plain", b"not found", close);
    }
    if path == "/" || path == "/index.html" {
        return send(w, 200, "text/html; charset=utf-8", DASHBOARD.as_bytes(), close);
    }
    if path == "/api/status" {
        return send(w, 200, "application/json", status_json(p).as_bytes(), close);
    }
    send(w, 404, "text/plain", b"not found", close)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_escaping() {
        assert_eq!(json_str("a\"b\\c\n\u{1}é"), "\"a\\\"b\\\\c\\n\\u0001é\"");
    }

    #[test]
    fn query() {
        assert_eq!(query_get("state=jam&x=1", "state").as_deref(), Some("jam"));
        assert_eq!(query_get("a=1&state=paper%6Fut", "state").as_deref(), Some("paperout"));
        assert_eq!(query_get("a=1", "state"), None);
        assert_eq!(pct_decode("%zz%4"), "%zz%4");
    }
}
