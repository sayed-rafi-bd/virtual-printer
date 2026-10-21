//! virtualprinter - a multi-protocol virtual network printer (Rust port of virtualprinter.py, no dashboard).
//!
//! Listens on (default --offset 11000, so no root is needed):
//!   20100  Raw / JetDirect  (also answers PJL, ESC/POS and ZPL status queries)
//!   11515  LPD / LPR
//!   11631  IPP  (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, ...)
//!   11080  HTTP: web dashboard, JSON status (/api/status) and state control (GET /set?state=jam)
//!
//! Every received job is saved under ./jobs/.

mod http;
mod ipp;
mod lpd;
mod printer;
mod raw;

use std::env;
use std::io::ErrorKind;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use printer::{Printer, State};

const USAGE: &str = "\
Usage: virtualprinter [options]

  --host HOST     address to bind (default 0.0.0.0)
  --offset N      add N to every port (default 11000 = high ports, no root;
                  use 0 for the standard 9100/515/631/80)
  --jobs DIR      directory for received jobs (default: jobs)
  --state STATE   initial state: ok, paperout, jam, offline, hang, drop (default: ok)
  --name NAME     printer name (default: Virtual Printer)
  --idle SECS     seconds of silence that end a raw job (default: 2.0)
  -h, --help      show this help

States can be changed at runtime:  curl 'http://localhost:11080/set?state=jam'
  ok        ready
  paperout  status queries report paper out
  jam       status queries report a paper jam
  offline   status queries report offline
  hang      accept connections, never answer  (timeout testing)
  drop      close connections immediately     (connection error testing)
";

struct Args {
    host: String,
    offset: u32,
    jobs: PathBuf,
    state: State,
    name: String,
    idle: f64,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        host: "0.0.0.0".into(),
        offset: 11000,
        jobs: PathBuf::from("jobs"),
        state: State::Ready,
        name: "Virtual Printer".into(),
        idle: 2.0,
    };
    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        if arg == "-h" || arg == "--help" {
            print!("{}", USAGE);
            process::exit(0);
        }
        let (key, inline) = match arg.split_once('=') {
            Some((k, v)) if k.starts_with("--") => (k.to_string(), Some(v.to_string())),
            _ => (arg.clone(), None),
        };
        let mut value = |key: &str| -> Result<String, String> {
            match inline.clone() {
                Some(v) => Ok(v),
                None => it.next().ok_or_else(|| format!("{} needs a value", key)),
            }
        };
        match key.as_str() {
            "--host" => a.host = value(&key)?,
            "--offset" => a.offset = value(&key)?.parse().map_err(|_| "--offset must be a non-negative integer".to_string())?,
            "--jobs" => a.jobs = PathBuf::from(value(&key)?),
            "--state" => {
                let s = value(&key)?;
                a.state = State::parse(&s).ok_or_else(|| format!("--state must be one of: {}", State::names()))?;
            }
            "--name" => a.name = value(&key)?,
            "--idle" => {
                a.idle = value(&key)?.parse().map_err(|_| "--idle must be a number".to_string())?;
                if !a.idle.is_finite() || a.idle <= 0.0 {
                    return Err("--idle must be greater than 0".into());
                }
            }
            other => return Err(format!("unknown argument: {}", other)),
        }
    }
    Ok(a)
}

#[derive(Clone, Copy)]
enum Kind {
    Raw,
    Lpd,
    Http, // serves both the IPP port and the status/control port
}

fn serve(p: Arc<Printer>, listener: TcpListener, kind: Kind) {
    thread::spawn(move || {
        for conn in listener.incoming() {
            match conn {
                Ok(stream) => {
                    let p = Arc::clone(&p);
                    thread::spawn(move || match kind {
                        Kind::Raw => raw::handle(&p, stream),
                        Kind::Lpd => lpd::handle(&p, stream),
                        Kind::Http => http::handle(&p, stream),
                    });
                }
                Err(_) => thread::sleep(Duration::from_millis(10)),
            }
        }
    });
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {}\n\n{}", e, USAGE);
            process::exit(2);
        }
    };
    let printer = Arc::new(Printer::new(
        args.name.clone(),
        args.jobs.clone(),
        Duration::from_secs_f64(args.idle),
        args.state,
    ));

    let services: [(&'static str, u32, Kind); 4] = [
        ("raw/JetDirect", 9100, Kind::Raw),
        ("LPD", 515, Kind::Lpd),
        ("IPP", 631, Kind::Http),
        ("HTTP", 80, Kind::Http),
    ];
    for (name, base, kind) in services {
        let port = base + args.offset;
        if port > 65535 {
            eprintln!("! {}: port {} is out of range, lower --offset", name, port);
            continue;
        }
        let port = port as u16;
        match TcpListener::bind((args.host.as_str(), port)) {
            Ok(l) => {
                printer.add_port(name, port);
                serve(Arc::clone(&printer), l, kind);
                println!("  {:14} listening on {}:{}", name, args.host, port);
            }
            Err(e) if e.kind() == ErrorKind::PermissionDenied => {
                eprintln!("! {}: port {} needs root. Re-run with sudo or use a higher --offset", name, port);
            }
            Err(e) => eprintln!("! {}: port {}: {}", name, port, e),
        }
    }
    if printer.ports().is_empty() {
        eprintln!("no listener could be started");
        process::exit(1);
    }
    let abs = std::fs::canonicalize(&args.jobs).unwrap_or_else(|_| env::current_dir().unwrap_or_default().join(&args.jobs));
    println!("State: {}   Jobs saved to: {}", printer.state().name(), abs.display());
    if let Some(web) = printer.port("HTTP") {
        println!("Dashboard: http://localhost:{}/   (state control: GET /set?state=jam)", web);
    }
    loop {
        thread::park();
    }
}
