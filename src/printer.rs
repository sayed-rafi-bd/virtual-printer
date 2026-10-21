//! Shared printer state, job storage and small time helpers (std only).

use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum State {
    Ready, // "ok"
    PaperOut,
    Jam,
    Offline,
    Hang, // accept connections, never answer
    Drop, // close connections immediately
}

impl State {
    pub const ALL: [State; 6] = [
        State::Ready,
        State::PaperOut,
        State::Jam,
        State::Offline,
        State::Hang,
        State::Drop,
    ];

    pub fn name(self) -> &'static str {
        match self {
            State::Ready => "ok",
            State::PaperOut => "paperout",
            State::Jam => "jam",
            State::Offline => "offline",
            State::Hang => "hang",
            State::Drop => "drop",
        }
    }

    pub fn parse(s: &str) -> Option<State> {
        State::ALL.iter().copied().find(|st| st.name() == s)
    }

    pub fn names() -> String {
        State::ALL.iter().map(|s| s.name()).collect::<Vec<_>>().join(", ")
    }
}

#[derive(Clone)]
pub struct JobRecord {
    pub id: u64,
    pub proto: &'static str,
    pub file: String,
    pub size: usize,
    /// HH:MM:SS (UTC) when the job was saved
    pub time: String,
}

struct Jobs {
    next_id: u64,
    list: Vec<JobRecord>,
}

pub struct Printer {
    pub name: String,
    pub jobdir: PathBuf,
    /// seconds of silence that end a raw job
    pub idle: Duration,
    started: Instant,
    state: Mutex<State>,
    jobs: Mutex<Jobs>,
    ports: Mutex<Vec<(&'static str, u16)>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Printer {
    pub fn new(name: String, jobdir: PathBuf, idle: Duration, state: State) -> Printer {
        Printer {
            name,
            jobdir,
            idle,
            started: Instant::now(),
            state: Mutex::new(state),
            jobs: Mutex::new(Jobs { next_id: 1, list: Vec::new() }),
            ports: Mutex::new(Vec::new()),
        }
    }

    pub fn state(&self) -> State {
        *lock(&self.state)
    }

    pub fn set_state(&self, s: State) {
        *lock(&self.state) = s;
    }

    pub fn add_port(&self, name: &'static str, port: u16) {
        lock(&self.ports).push((name, port));
    }

    pub fn ports(&self) -> Vec<(&'static str, u16)> {
        lock(&self.ports).clone()
    }

    pub fn port(&self, name: &str) -> Option<u16> {
        lock(&self.ports).iter().find(|(n, _)| *n == name).map(|(_, p)| *p)
    }

    /// Save a received job under the job directory. Returns the job id.
    pub fn save_job(&self, proto: &'static str, data: &[u8]) -> io::Result<u64> {
        fs::create_dir_all(&self.jobdir)?;
        let mut jobs = lock(&self.jobs);
        let id = jobs.next_id;
        jobs.next_id += 1;
        let fname = format!("{}_{}_{}.{}", proto, file_timestamp(), id, sniff_ext(data));
        let path = self.jobdir.join(&fname);
        fs::write(&path, data)?;
        jobs.list.push(JobRecord { id, proto, file: fname, size: data.len(), time: clock_hms() });
        drop(jobs);
        println!("[{}] job {}: {} bytes -> {}", proto, id, data.len(), path.display());
        Ok(id)
    }

    /// Ids of the most recent `n` jobs, oldest first.
    pub fn recent_job_ids(&self, n: usize) -> Vec<u64> {
        let jobs = lock(&self.jobs);
        let start = jobs.list.len().saturating_sub(n);
        jobs.list[start..].iter().map(|j| j.id).collect()
    }

    /// The most recent `n` jobs, oldest first.
    pub fn recent_jobs(&self, n: usize) -> Vec<JobRecord> {
        let jobs = lock(&self.jobs);
        let start = jobs.list.len().saturating_sub(n);
        jobs.list[start..].to_vec()
    }

    /// (job count, total bytes received)
    pub fn job_stats(&self) -> (usize, usize) {
        let jobs = lock(&self.jobs);
        (jobs.list.len(), jobs.list.iter().map(|j| j.size).sum())
    }

    pub fn uptime_string(&self) -> String {
        let up = self.started.elapsed().as_secs();
        let days = if up >= 86400 { format!("{}d ", up / 86400) } else { String::new() };
        format!("{}{}h {}m {}s", days, up % 86400 / 3600, up % 3600 / 60, up % 60)
    }
}

// --------------------------------------------------------------------------
// file type sniffing
// --------------------------------------------------------------------------
const UEL: &[u8] = b"\x1b%-12345X";

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    !needle.is_empty() && hay.windows(needle.len()).any(|w| w == needle)
}

fn is_py_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// Equivalent of the regex `^(\x1b%-12345X)?(@PJL[^\n]*\n)*\s*%!PS`.
fn is_postscript(data: &[u8]) -> bool {
    let mut i = 0;
    if data[i..].starts_with(UEL) {
        i += UEL.len();
    }
    while data[i..].starts_with(b"@PJL") {
        match data[i..].iter().position(|&b| b == b'\n') {
            Some(nl) => i += nl + 1,
            None => return false,
        }
    }
    while i < data.len() && is_py_space(data[i]) {
        i += 1;
    }
    data[i..].starts_with(b"%!PS")
}

pub fn sniff_ext(data: &[u8]) -> &'static str {
    let head = |n: usize| &data[..n.min(data.len())];
    if data.starts_with(b"%PDF") {
        return "pdf";
    }
    if data.starts_with(b"%!PS") || is_postscript(data) {
        return "ps";
    }
    if contains(head(16), UEL) || contains(head(64), b"@PJL") {
        return "pjl";
    }
    let lstripped = data.iter().position(|&b| !is_py_space(b)).map(|i| &data[i..]).unwrap_or(&[]);
    if lstripped.starts_with(b"^XA") {
        return "zpl";
    }
    if contains(head(8), b"\x1b@") || contains(data, b"\x1dV") {
        return "escpos";
    }
    if data.starts_with(b"UNIRAST") || data.starts_with(b"RaS2") {
        return "ras";
    }
    "bin"
}

// --------------------------------------------------------------------------
// time helpers (UTC; no external crates)
// --------------------------------------------------------------------------
fn now_secs() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// Days since 1970-01-01 -> (year, month, day). Howard Hinnant's algorithm.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `YYYYmmdd_HHMMSS` (UTC), used in job file names.
pub fn file_timestamp() -> String {
    let secs = now_secs();
    let (y, m, d) = civil_from_days(secs.div_euclid(86_400));
    let t = secs.rem_euclid(86_400);
    format!("{:04}{:02}{:02}_{:02}{:02}{:02}", y, m, d, t / 3600, t % 3600 / 60, t % 60)
}

/// `HH:MM:SS` (UTC).
pub fn clock_hms() -> String {
    let t = now_secs().rem_euclid(86_400);
    format!("{:02}:{:02}:{:02}", t / 3600, t % 3600 / 60, t % 60)
}

/// RFC 7231 `Date:` header value.
pub fn http_date() -> String {
    const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const MONTHS: [&str; 12] =
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let secs = now_secs();
    let days = secs.div_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    let t = secs.rem_euclid(86_400);
    let wd = (days + 4).rem_euclid(7) as usize; // 1970-01-01 was a Thursday
    format!(
        "{}, {:02} {} {} {:02}:{:02}:{:02} GMT",
        DAYS[wd], d, MONTHS[(m - 1) as usize], y, t / 3600, t % 3600 / 60, t % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sniff() {
        assert_eq!(sniff_ext(b"%PDF-1.7 ..."), "pdf");
        assert_eq!(sniff_ext(b"%!PS-Adobe-3.0"), "ps");
        assert_eq!(sniff_ext(b"\x1b%-12345X@PJL JOB\r\n@PJL ENTER LANGUAGE=POSTSCRIPT\r\n%!PS\n"), "ps");
        assert_eq!(sniff_ext(b"\x1b%-12345X@PJL JOB\r\nPCLDATA"), "pjl");
        assert_eq!(sniff_ext(b"  \n^XA^FO50,50^XZ"), "zpl");
        assert_eq!(sniff_ext(b"\x1b@hello"), "escpos");
        assert_eq!(sniff_ext(b"hello\x1dV\x00"), "escpos");
        assert_eq!(sniff_ext(b"UNIRASTER"), "ras");
        assert_eq!(sniff_ext(b"hello"), "bin");
        assert_eq!(sniff_ext(b""), "bin");
    }

    #[test]
    fn dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
    }
}
