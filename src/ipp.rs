//! IPP: Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, Cancel-Job, Get-Job-Attributes.

use crate::printer::{Printer, State};

const DOC_FORMATS: [&str; 7] = [
    "application/pdf",
    "application/postscript",
    "application/vnd.cups-raw",
    "image/jpeg",
    "image/pwg-raster",
    "image/urf",
    "text/plain",
];

// value tags
const T_INTEGER: u8 = 0x21;
const T_BOOLEAN: u8 = 0x22;
const T_ENUM: u8 = 0x23;
const T_TEXT: u8 = 0x41;
const T_NAME: u8 = 0x42;
const T_KEYWORD: u8 = 0x44;
const T_URI: u8 = 0x45;
const T_CHARSET: u8 = 0x47;
const T_LANGUAGE: u8 = 0x48;
const T_MIME: u8 = 0x49;

fn attr(out: &mut Vec<u8>, tag: u8, name: &str, val: &[u8]) {
    out.push(tag);
    out.extend_from_slice(&(name.len() as u16).to_be_bytes());
    out.extend_from_slice(name.as_bytes());
    out.extend_from_slice(&(val.len() as u16).to_be_bytes());
    out.extend_from_slice(val);
}

/// Multi-valued attribute: first value carries the name, the rest have an empty name.
fn multi(out: &mut Vec<u8>, tag: u8, name: &str, vals: &[&[u8]]) {
    for (i, v) in vals.iter().enumerate() {
        attr(out, tag, if i == 0 { name } else { "" }, v);
    }
}

fn i32b(n: i32) -> [u8; 4] {
    n.to_be_bytes()
}

struct Request {
    version: (u8, u8),
    op: u16,
    request_id: u32,
    data_offset: usize,
}

/// Parse the IPP header and skip the attribute groups; the document data starts at `data_offset`.
fn parse(b: &[u8]) -> Result<Request, String> {
    if b.len() < 8 {
        return Err("truncated IPP header".into());
    }
    let op = u16::from_be_bytes([b[2], b[3]]);
    let request_id = u32::from_be_bytes([b[4], b[5], b[6], b[7]]);
    let mut i = 8;
    while i < b.len() {
        let tag = b[i];
        if tag == 0x03 {
            i += 1; // end-of-attributes
            break;
        }
        if tag <= 0x05 {
            i += 1; // group delimiter
            continue;
        }
        if i + 3 > b.len() {
            return Err("truncated attribute name length".into());
        }
        let nl = u16::from_be_bytes([b[i + 1], b[i + 2]]) as usize;
        let j = i + 3 + nl;
        if j + 2 > b.len() {
            return Err("truncated attribute value length".into());
        }
        let vl = u16::from_be_bytes([b[j], b[j + 1]]) as usize;
        i = j + 2 + vl;
    }
    Ok(Request { version: (b[0], b[1]), op, request_id, data_offset: i.min(b.len()) })
}

pub fn handle(p: &Printer, body: &[u8], uri: &str) -> Result<Vec<u8>, String> {
    let req = parse(body)?;
    let st = p.state();
    let (pstate, reason): (i32, &str) = match st {
        State::PaperOut => (5, "media-empty"),
        State::Jam => (5, "media-jam"),
        State::Offline => (5, "offline-report"),
        _ => (3, "none"),
    };

    // version(2) + status-code(2) + request-id(4) + operation attributes group
    let respond = |status: [u8; 2], groups: &[u8]| -> Vec<u8> {
        let mut out = vec![req.version.0, req.version.1, status[0], status[1]];
        out.extend_from_slice(&req.request_id.to_be_bytes());
        out.push(0x01);
        attr(&mut out, T_CHARSET, "attributes-charset", b"utf-8");
        attr(&mut out, T_LANGUAGE, "attributes-natural-language", b"en");
        out.extend_from_slice(groups);
        out.push(0x03);
        out
    };
    let ok = [0x00, 0x00];

    match req.op {
        0x000B => {
            // Get-Printer-Attributes
            let mut g = vec![0x04];
            attr(&mut g, T_URI, "printer-uri-supported", uri.as_bytes());
            attr(&mut g, T_KEYWORD, "uri-security-supported", b"none");
            attr(&mut g, T_KEYWORD, "uri-authentication-supported", b"none");
            attr(&mut g, T_NAME, "printer-name", p.name.as_bytes());
            attr(&mut g, T_TEXT, "printer-make-and-model", p.name.as_bytes());
            attr(&mut g, T_TEXT, "printer-info", p.name.as_bytes());
            attr(&mut g, T_ENUM, "printer-state", &i32b(pstate));
            attr(&mut g, T_KEYWORD, "printer-state-reasons", reason.as_bytes());
            attr(&mut g, T_BOOLEAN, "printer-is-accepting-jobs", &[1]);
            attr(&mut g, T_INTEGER, "queued-job-count", &i32b(0));
            multi(&mut g, T_KEYWORD, "ipp-versions-supported", &[b"1.0", b"1.1", b"2.0"]);
            let ops: Vec<[u8; 4]> = [0x02, 0x04, 0x08, 0x09, 0x0A, 0x0B].iter().map(|&x| i32b(x)).collect();
            let ops: Vec<&[u8]> = ops.iter().map(|o| &o[..]).collect();
            multi(&mut g, T_ENUM, "operations-supported", &ops);
            attr(&mut g, T_CHARSET, "charset-configured", b"utf-8");
            attr(&mut g, T_CHARSET, "charset-supported", b"utf-8");
            attr(&mut g, T_LANGUAGE, "natural-language-configured", b"en");
            attr(&mut g, T_LANGUAGE, "generated-natural-language-supported", b"en");
            attr(&mut g, T_MIME, "document-format-default", b"application/octet-stream");
            let fmts: Vec<&[u8]> = DOC_FORMATS.iter().map(|f| f.as_bytes()).collect();
            multi(&mut g, T_MIME, "document-format-supported", &fmts);
            attr(&mut g, T_KEYWORD, "pdl-override-supported", b"not-attempted");
            attr(&mut g, T_KEYWORD, "compression-supported", b"none");
            attr(&mut g, T_BOOLEAN, "color-supported", &[1]);
            Ok(respond(ok, &g))
        }
        0x0004 => Ok(respond(ok, &[])), // Validate-Job
        0x0002 => {
            // Print-Job
            let data = &body[req.data_offset..];
            let jid = p.save_job("ipp", data).map_err(|e| format!("cannot save job: {}", e))?;
            let done = st == State::Ready;
            let mut g = vec![0x02];
            attr(&mut g, T_INTEGER, "job-id", &i32b(jid as i32));
            attr(&mut g, T_URI, "job-uri", format!("{}/jobs/{}", uri, jid).as_bytes());
            attr(&mut g, T_ENUM, "job-state", &i32b(if done { 9 } else { 6 }));
            attr(
                &mut g,
                T_KEYWORD,
                "job-state-reasons",
                if done { b"job-completed-successfully" as &[u8] } else { b"printer-stopped" },
            );
            Ok(respond(ok, &g))
        }
        0x000A => {
            // Get-Jobs
            let mut g = Vec::new();
            for jid in p.recent_job_ids(20) {
                g.push(0x02);
                attr(&mut g, T_INTEGER, "job-id", &i32b(jid as i32));
                attr(&mut g, T_ENUM, "job-state", &i32b(9));
            }
            Ok(respond(ok, &g))
        }
        0x0008 | 0x0009 => Ok(respond(ok, &[])), // Cancel-Job / Get-Job-Attributes
        _ => Ok(respond([0x05, 0x01], &[])),      // server-error-operation-not-supported
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::Duration;

    fn printer() -> Printer {
        Printer::new("T".into(), PathBuf::from("/tmp/vp-test-jobs"), Duration::from_secs(1), State::Ready)
    }

    #[test]
    fn rejects_truncated() {
        assert!(handle(&printer(), &[1, 1, 0], "ipp://x/y").is_err());
        // attribute claims a name but the buffer ends
        assert!(handle(&printer(), &[1, 1, 0, 0x0b, 0, 0, 0, 1, 0x47, 0, 10, b'a'], "ipp://x/y").is_err());
    }

    #[test]
    fn unsupported_op() {
        let r = handle(&printer(), &[1, 1, 0x40, 0x00, 0, 0, 0, 7, 0x03], "ipp://x/y").unwrap();
        assert_eq!(&r[..8], &[1, 1, 0x05, 0x01, 0, 0, 0, 7]);
        assert_eq!(*r.last().unwrap(), 0x03);
    }
}
