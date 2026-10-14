#!/usr/bin/env python3
"""
virtualprinter.py - Virtual Printer: a multi-protocol virtual network printer (Python 3.8+, stdlib only).

Listens on:
  9100  Raw / JetDirect  (also answers PJL, ESC/POS and ZPL status queries)
  515   LPD / LPR
  631   IPP  (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, ...)
  80    HTTP status page + state control  (GET /set?state=jam)

Every received job is saved under ./jobs/.

Run:   sudo python3 virtualprinter.py            (real ports, needs root for <1024)
       python3 virtualprinter.py --offset 10000  (no root: 19100, 10515, 10631, 10080)

States (change at runtime with  curl 'http://localhost/set?state=jam'):
  ok        ready
  paperout  status queries report paper out
  jam       status queries report a paper jam
  offline   status queries report offline
  hang      accept connections, never answer  (timeout testing)
  drop      close connections immediately     (connection error testing)
"""
import argparse
import html as htmllib
import ipaddress
import itertools
import webbrowser
import os
import re
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

STATES = ("ok", "paperout", "jam", "offline", "hang", "drop")
STATE = {"value": "ok"}
CFG = {"jobdir": "jobs", "name": "Virtual Printer", "idle": 2.0, "ports": [], "started": time.time()}
COUNTER = itertools.count(1)
JOBS = []  # (id, protocol, filename, size)
LOCK = threading.Lock()


# --------------------------------------------------------------------------
# address discovery (public IP shown on the dashboard instead of localhost)
# --------------------------------------------------------------------------
IP = {"public": None, "lan": None, "override": None, "lookup": True}
IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")


def lan_ip():
    """Address of the interface used for outbound traffic (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def fetch_public_ip():
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "virtualprinter"})
            with urllib.request.urlopen(req, timeout=4) as r:
                ip = r.read().decode().strip()
            ipaddress.ip_address(ip)  # reject anything that is not an IP
            return ip
        except Exception:
            continue
    return None


def public_ip_loop():
    while True:
        ip = fetch_public_ip()
        if ip:
            IP["public"] = ip
        time.sleep(600 if ip else 60)  # re-check: public IPs change, and retry quickly on failure


def display_host():
    """(host, source) used for the connection URIs on the dashboard."""
    if IP["override"]:
        return IP["override"], "manual"
    if IP["public"]:
        return IP["public"], "public"
    return IP["lan"] or "127.0.0.1", "lan"


# --------------------------------------------------------------------------
# job storage
# --------------------------------------------------------------------------
def sniff_ext(data):
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"%!PS") or re.match(rb"(\x1b%-12345X)?(@PJL[^\n]*\n)*\s*%!PS", data):
        return "ps"
    if b"\x1b%-12345X" in data[:16] or b"@PJL" in data[:64]:
        return "pjl"
    if data.lstrip().startswith(b"^XA"):
        return "zpl"
    if b"\x1b@" in data[:8] or b"\x1dV" in data:
        return "escpos"
    if data.startswith(b"UNIRAST") or data.startswith(b"RaS2"):
        return "ras"
    return "bin"


def save_job(proto, data):
    os.makedirs(CFG["jobdir"], exist_ok=True)
    with LOCK:
        jid = next(COUNTER)
        fn = os.path.join(CFG["jobdir"], f"{proto}_{time.strftime('%Y%m%d_%H%M%S')}_{jid}.{sniff_ext(data)}")
        with open(fn, "wb") as f:
            f.write(data)
        JOBS.append((jid, proto, fn, len(data)))
    print(f"[{proto}] job {jid}: {len(data)} bytes -> {fn}", flush=True)
    return jid


# --------------------------------------------------------------------------
# status replies for raw port (representative codes: adjust to your target)
# --------------------------------------------------------------------------
PJL = {
    "ok": (10001, "Ready", "TRUE"),
    "paperout": (30010, "Load Paper", "TRUE"),
    "jam": (30016, "Paper Jam", "TRUE"),
    "offline": (10023, "Offline", "FALSE"),
}

QUERY_RE = re.compile(
    rb"(?s)(\x1b%-12345X)|@PJL (INFO \w+|USTATUS \w+=\w+|ECHO[^\r\n]*|INQUIRE \w+|DINQUIRE \w+)\r?\n?"
    rb"|\x10\x04.|\x10\x05.|~HS|~HI|~HQES|~HQ\w+"
)


def pjl_reply(cmd):
    code, disp, online = PJL[STATE["value"]]
    if cmd.startswith("INFO STATUS") or cmd.startswith("USTATUS"):
        return (f'@PJL INFO STATUS\r\nCODE={code}\r\nDISPLAY="{disp}"\r\nONLINE={online}\r\n\f').encode()
    if cmd.startswith("INFO ID"):
        return f'@PJL INFO ID\r\n"{CFG["name"]}"\r\n\f'.encode()
    if cmd.startswith("INFO PAGECOUNT"):
        return b"@PJL INFO PAGECOUNT\r\n12345\r\n\f"
    if cmd.startswith("INFO CONFIG"):
        return b"@PJL INFO CONFIG\r\nLANGUAGES=PCL PS PDF\r\nMEMORY=256MB\r\n\f"
    if cmd.startswith("ECHO"):
        return ("@PJL " + cmd + "\r\n\f").encode()
    return b""


def escpos_reply(n):
    st = STATE["value"]
    ok = 0x12
    if n == 1:  # printer status
        return bytes([0x1E if st == "offline" else ok])
    if n == 2:  # offline cause
        return bytes([{"jam": 0x52}.get(st, ok)])
    if n == 3:  # error cause
        return bytes([{"jam": 0x32}.get(st, ok)])
    if n == 4:  # paper sensor
        return bytes([{"paperout": 0x72}.get(st, ok)])
    return bytes([ok])


def zpl_hs():
    paper_out = 1 if STATE["value"] == "paperout" else 0
    pause = 1 if STATE["value"] == "offline" else 0
    l1 = f"030,{paper_out},{pause},1234,000,0,0,0,000,0,0,0"
    l2 = "000,0,0,0,0,2,4,0,00000000,1,000"
    l3 = "0000,0"
    return "".join(f"\x02{l}\x03\r\n" for l in (l1, l2, l3)).encode()


def raw_replies(chunk):
    out = []
    for m in QUERY_RE.finditer(chunk):
        tok = m.group(0)
        if tok.startswith(b"@PJL"):
            out.append(pjl_reply(m.group(2).decode(errors="replace")))
        elif tok.startswith(b"\x10\x04"):
            out.append(escpos_reply(tok[2]))
        elif tok.startswith(b"\x10\x05"):
            out.append(b"")
        elif tok == b"~HS":
            out.append(zpl_hs())
        elif tok == b"~HI":
            out.append(f"\x02{CFG['name']},V75.20.01Z,203,8176KB\x03\r\n".encode())
        elif tok == b"~HQES":
            out.append(b"\x02\r\n  PRINTER STATUS\r\n  ERRORS: 0 00000000 00000000\r\n  WARNINGS: 0 00000000 00000000\r\n\x03\r\n")
    return [o for o in out if o]


# --------------------------------------------------------------------------
# Raw / JetDirect
# --------------------------------------------------------------------------
class Raw(socketserver.BaseRequestHandler):
    def handle(self):
        st = STATE["value"]
        if st == "drop":
            return
        if st == "hang":
            time.sleep(60)
            return
        s = self.request
        s.settimeout(CFG["idle"])
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            try:
                for r in raw_replies(chunk):
                    s.sendall(r)
            except OSError:
                break
        # query-only sessions are not jobs
        if QUERY_RE.sub(b"", buf).strip(b"\r\n\f \x00"):
            save_job("raw", buf)
        else:
            print(f"[raw] status query from {self.client_address[0]}", flush=True)


# --------------------------------------------------------------------------
# LPD / LPR  (RFC 1179)
# --------------------------------------------------------------------------
class LPD(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        st = STATE["value"]
        if st == "drop":
            return
        if st == "hang":
            time.sleep(60)
            return
        line = self.rfile.readline()
        if not line:
            return
        cmd, queue = line[0], line[1:].strip().decode(errors="replace")
        if cmd in (3, 4):  # short / long queue state
            self.wfile.write(f"{queue} is ready\nno entries\n".encode())
            return
        if cmd != 2:  # 1 = print waiting jobs; anything else ignored
            return
        self._ack()
        data = b""
        while True:
            hdr = self.rfile.readline()
            if not hdr or hdr[0] == 1:
                break
            if hdr[0] in (2, 3):
                count = int(hdr[1:].split(b" ")[0])
                self._ack()
                blob = self.rfile.read(count)
                self.rfile.read(1)
                self._ack()
                if hdr[0] == 3:
                    data += blob
            else:
                break
        if data:
            save_job("lpd", data)

    def _ack(self):
        self.wfile.write(b"\0")
        self.wfile.flush()


# --------------------------------------------------------------------------
# IPP
# --------------------------------------------------------------------------
IPP_STATE = {  # printer-state enum, reasons
    "ok": (3, "none"),
    "paperout": (5, "media-empty"),
    "jam": (5, "media-jam"),
    "offline": (5, "offline-report"),
}
DOC_FORMATS = ["application/pdf", "application/postscript", "application/vnd.cups-raw",
               "image/jpeg", "image/pwg-raster", "image/urf", "text/plain"]


def a(tag, name, val):
    if isinstance(val, str):
        val = val.encode()
    return bytes([tag]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(val)) + val


def multi(tag, name, vals):
    out = a(tag, name, vals[0])
    for v in vals[1:]:
        out += a(tag, "", v)
    return out


def i32(n):
    return struct.pack(">i", n)


def parse_ipp(b):
    ver = (b[0], b[1])
    op, rid = struct.unpack(">HI", b[2:8])
    i, attrs = 8, {}
    while i < len(b):
        tag = b[i]
        if tag == 0x03:
            i += 1
            break
        if tag <= 0x05:
            i += 1
            continue
        nl = struct.unpack(">H", b[i + 1:i + 3])[0]
        name = b[i + 3:i + 3 + nl].decode(errors="replace")
        j = i + 3 + nl
        vl = struct.unpack(">H", b[j:j + 2])[0]
        val = b[j + 2:j + 2 + vl]
        if name:
            attrs[name] = val
        i = j + 2 + vl
    return ver, op, rid, attrs, b[i:]


def handle_ipp(body, uri):
    ver, op, rid, attrs, data = parse_ipp(body)
    head = bytes(ver) + b"%s" + struct.pack(">I", rid)
    op_group = bytes([0x01]) + a(0x47, "attributes-charset", "utf-8") + a(0x48, "attributes-natural-language", "en")
    st = STATE["value"]
    pstate, reason = IPP_STATE.get(st, IPP_STATE["ok"])

    if op == 0x000B:  # Get-Printer-Attributes
        g = bytes([0x04])
        g += a(0x45, "printer-uri-supported", uri)
        g += a(0x44, "uri-security-supported", "none")
        g += a(0x44, "uri-authentication-supported", "none")
        g += a(0x42, "printer-name", CFG["name"])
        g += a(0x41, "printer-make-and-model", CFG["name"])
        g += a(0x41, "printer-info", CFG["name"])
        g += a(0x23, "printer-state", i32(pstate))
        g += a(0x44, "printer-state-reasons", reason)
        g += a(0x22, "printer-is-accepting-jobs", b"\x01")
        g += a(0x21, "queued-job-count", i32(0))
        g += multi(0x44, "ipp-versions-supported", ["1.0", "1.1", "2.0"])
        g += multi(0x23, "operations-supported", [i32(x) for x in (0x02, 0x04, 0x08, 0x09, 0x0A, 0x0B)])
        g += a(0x47, "charset-configured", "utf-8")
        g += a(0x47, "charset-supported", "utf-8")
        g += a(0x48, "natural-language-configured", "en")
        g += a(0x48, "generated-natural-language-supported", "en")
        g += a(0x49, "document-format-default", "application/octet-stream")
        g += multi(0x49, "document-format-supported", DOC_FORMATS)
        g += a(0x44, "pdl-override-supported", "not-attempted")
        g += a(0x44, "compression-supported", "none")
        g += a(0x22, "color-supported", b"\x01")
        return head % b"\x00\x00" + op_group + g + b"\x03"

    if op in (0x0002, 0x0004):  # Print-Job / Validate-Job
        if op == 0x0004:
            return head % b"\x00\x00" + op_group + b"\x03"
        jid = save_job("ipp", data)
        jstate = 9 if st == "ok" else 6
        g = bytes([0x02]) + a(0x21, "job-id", i32(jid))
        g += a(0x45, "job-uri", f"{uri}/jobs/{jid}")
        g += a(0x23, "job-state", i32(jstate))
        g += a(0x44, "job-state-reasons", "job-completed-successfully" if st == "ok" else "printer-stopped")
        return head % b"\x00\x00" + op_group + g + b"\x03"

    if op == 0x000A:  # Get-Jobs
        g = b""
        for jid, proto, fn, size in JOBS[-20:]:
            g += bytes([0x02]) + a(0x21, "job-id", i32(jid)) + a(0x23, "job-state", i32(9))
        return head % b"\x00\x00" + op_group + g + b"\x03"

    if op in (0x0008, 0x0009):  # Cancel-Job / Get-Job-Attributes
        return head % b"\x00\x00" + op_group + b"\x03"

    return head % b"\x05\x01" + op_group + b"\x03"  # server-error-operation-not-supported


# --------------------------------------------------------------------------
# dashboard assets (fully self-contained: no CDN, works offline)
# --------------------------------------------------------------------------
APP_NAME = "Virtual Printer"

STATE_INFO = {  # state -> (pill label, short description)
    "ok": ("Ready", "Ready to print"),
    "paperout": ("Paper out", "Reports paper out"),
    "jam": ("Paper jam", "Reports a paper jam"),
    "offline": ("Offline", "Reports offline"),
    "hang": ("Hanging", "Never answers"),
    "drop": ("Dropping", "Closes connections"),
}

_ICONS = {
    "printer": "<polyline points='6 9 6 2 18 2 18 9'/><path d='M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2'/><rect x='6' y='14' width='12' height='8'/>",
    "sun": "<circle cx='12' cy='12' r='5'/><line x1='12' y1='1' x2='12' y2='3'/><line x1='12' y1='21' x2='12' y2='23'/><line x1='4.22' y1='4.22' x2='5.64' y2='5.64'/><line x1='18.36' y1='18.36' x2='19.78' y2='19.78'/><line x1='1' y1='12' x2='3' y2='12'/><line x1='21' y1='12' x2='23' y2='12'/><line x1='4.22' y1='19.78' x2='5.64' y2='18.36'/><line x1='18.36' y1='5.64' x2='19.78' y2='4.22'/>",
    "moon": "<path d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/>",
    "monitor": "<rect x='2' y='3' width='20' height='14' rx='2' ry='2'/><line x1='8' y1='21' x2='16' y2='21'/><line x1='12' y1='17' x2='12' y2='21'/>",
    "globe": "<circle cx='12' cy='12' r='10'/><line x1='2' y1='12' x2='22' y2='12'/><path d='M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z'/>",
    "clock": "<circle cx='12' cy='12' r='10'/><polyline points='12 6 12 12 16 14'/>",
    "file": "<path d='M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'/><polyline points='13 2 13 9 20 9'/>",
    "drive": "<line x1='22' y1='12' x2='2' y2='12'/><path d='M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'/><line x1='6' y1='16' x2='6.01' y2='16'/><line x1='10' y1='16' x2='10.01' y2='16'/>",
    "copy": "<rect x='9' y='9' width='13' height='13' rx='2' ry='2'/><path d='M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'/>",
    "check": "<polyline points='20 6 9 17 4 12'/>",
    "inbox": "<polyline points='22 12 16 12 14 15 10 15 8 12 2 12'/><path d='M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'/>",
}


def _icon(name):
    return ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' "
            f"stroke-linejoin='round' aria-hidden='true'>{_ICONS[name]}</svg>")


FAVICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='#4f46e5' stroke-width='2' "
    f"stroke-linecap='round' stroke-linejoin='round'>{_ICONS['printer']}</svg>")

# Theme tokens. Dark values are declared twice (OS preference + explicit choice), so they live in one string.
_DARK_TOKENS = (
    "color-scheme:dark;"
    "--bg:#0b0e14;--surface:#131823;--surface-2:#182030;--border:#242c3d;--border-strong:#34415b;"
    "--text:#e8ecf4;--muted:#97a3ba;"
    "--accent:#818cf8;--accent-fg:#0b0e14;--accent-soft:rgba(129,140,248,.14);--link:#93abff;"
    "--shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.22);--ring:rgba(129,140,248,.55);"
)

DASH_CSS = """
:root{color-scheme:light;
--bg:#f3f4f8;--surface:#fff;--surface-2:#f7f8fb;--border:#e3e6ee;--border-strong:#cdd3e0;
--text:#121826;--muted:#5d697e;
--accent:#4f46e5;--accent-fg:#fff;--accent-soft:#eceefe;--link:#3b5bdb;
--shadow:0 1px 2px rgba(18,24,38,.05),0 6px 20px rgba(18,24,38,.05);--ring:rgba(79,70,229,.45)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){%DARK%}}
:root[data-theme=dark]{%DARK%}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;
background-image:radial-gradient(900px 380px at 50% -140px,var(--accent-soft),transparent);background-repeat:no-repeat}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--link)}
:focus-visible{outline:2px solid var(--ring);outline-offset:2px;border-radius:6px}

.topbar{position:sticky;top:0;z-index:10;border-bottom:1px solid var(--border);
background:color-mix(in srgb,var(--bg) 80%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
.topbar-in{max-width:1120px;margin:0 auto;padding:10px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:15px;letter-spacing:-.01em}
.logo{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:var(--accent);color:var(--accent-fg)}
.logo svg{width:18px;height:18px}
.theme{display:inline-flex;gap:2px;padding:3px;background:var(--surface);border:1px solid var(--border);border-radius:11px}
.theme button{border:0;background:none;color:var(--muted);width:32px;height:28px;border-radius:8px;display:grid;place-items:center;cursor:pointer}
.theme button:hover{color:var(--text)}
.theme button[aria-pressed=true]{background:var(--accent-soft);color:var(--accent)}
.theme svg{width:16px;height:16px}

.wrap{max-width:1120px;margin:0 auto;padding:28px 24px 40px}
.hero{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}
.hero-l{display:flex;align-items:center;gap:16px;min-width:0}
.hero-icon{width:56px;height:56px;flex:none;border-radius:16px;display:grid;place-items:center;color:var(--accent);
background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow)}
.hero-icon svg{width:28px;height:28px}
h1{margin:0;font-size:26px;line-height:1.2;letter-spacing:-.02em;overflow-wrap:anywhere}
.sub{margin:2px 0 0;color:var(--muted)}

.s-ok{--c:#12b76a}.s-paperout{--c:#f79009}.s-jam{--c:#f04438}.s-offline{--c:#8792a8}.s-hang{--c:#8b5cf6}.s-drop{--c:#ec4899}
.p-raw{--c:#0ea5e9}.p-lpd{--c:#8b5cf6}.p-ipp{--c:#14b8a6}.p-x{--c:#8792a8}
.pill{display:inline-flex;align-items:center;gap:9px;padding:6px 14px;border-radius:999px;font-weight:600;font-size:13px;
color:color-mix(in srgb,var(--c) 62%,var(--text));background:color-mix(in srgb,var(--c) 13%,transparent);
border:1px solid color-mix(in srgb,var(--c) 30%,transparent)}
.dot{position:relative;display:inline-block;width:8px;height:8px;flex:none;border-radius:50%;background:var(--c)}
.dot.pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--c);animation:ping 1.8s cubic-bezier(0,0,.2,1) infinite}
@keyframes ping{0%{transform:scale(1);opacity:.55}80%,100%{transform:scale(2.7);opacity:0}}

.grid{display:grid;gap:16px}
.stats{grid-template-columns:repeat(4,minmax(0,1fr))}
.cols{grid-template-columns:minmax(0,3fr) minmax(0,2fr);margin-top:16px;align-items:start}
@media (max-width:900px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.cols{grid-template-columns:minmax(0,1fr)}}
@media (max-width:480px){.wrap,.topbar-in{padding-left:16px;padding-right:16px}h1{font-size:22px}.stats{gap:10px}.stat{flex-direction:column;gap:10px;padding:14px}.val{font-size:18px}}

.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow)}
.stat{display:flex;gap:14px;align-items:flex-start;padding:18px}
.stat-ic{width:38px;height:38px;flex:none;border-radius:11px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent)}
.stat-ic svg{width:18px;height:18px}
.stat>div:last-child{min-width:0}
.label{font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.val{margin-top:2px;font-size:21px;line-height:1.25;font-weight:650;letter-spacing:-.01em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.hint{font-size:12px;color:var(--muted)}

.panel{padding:20px 22px}
.panel h2,.jobs-h h2{margin:0;font-size:15px;font-weight:650}
.panel h2{margin-bottom:14px}
.ep{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--border)}
.ep:first-child{border-top:0;padding-top:0}
.ep>div{min-width:0}
.ep-v{margin-top:2px;font-size:13px;overflow-wrap:anywhere}
.btn{display:inline-flex;align-items:center;gap:6px;flex:none;padding:6px 10px;border-radius:9px;cursor:pointer;
border:1px solid var(--border-strong);background:var(--surface);color:var(--text);font:inherit;font-size:12.5px;font-weight:550}
.btn:hover{background:var(--surface-2)}
.btn svg{width:14px;height:14px}
.btn.done{color:#12b76a;border-color:currentColor}
.note{margin-top:16px;padding:11px 13px;border-radius:10px;font-size:12.5px;background:var(--surface-2);border:1px solid var(--border);color:var(--muted)}
.note.warn{--c:#f79009;color:color-mix(in srgb,var(--c) 55%,var(--text));background:color-mix(in srgb,var(--c) 11%,transparent);border-color:color-mix(in srgb,var(--c) 32%,transparent)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}

.states{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sbtn{display:flex;flex-direction:column;gap:2px;padding:10px 12px;border-radius:12px;text-decoration:none;
border:1px solid var(--border);background:var(--surface);color:var(--text);transition:background .15s,border-color .15s}
.sbtn:hover{background:var(--surface-2);border-color:var(--border-strong)}
.sbtn b{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:600}
.sbtn small{color:var(--muted);font-size:12px}
.sbtn .chk{margin-left:auto;width:14px;height:14px;color:var(--c)}
.sbtn[aria-current=true]{border-color:var(--c);box-shadow:inset 0 0 0 1px var(--c);background:color-mix(in srgb,var(--c) 9%,var(--surface))}
.help{margin:14px 0 0;font-size:12px;line-height:1.6;color:var(--muted)}

.jobs{margin-top:16px;overflow:hidden}
.jobs-h{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 12px;padding:16px 22px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:12px;font-weight:600}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{padding:10px 22px;text-align:left;white-space:nowrap;background:var(--surface-2);color:var(--muted);
font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
td{padding:12px 22px;white-space:nowrap;border-top:1px solid var(--border)}
tbody tr:hover{background:var(--surface-2)}
.r{text-align:right}.num{font-variant-numeric:tabular-nums}.muted{color:var(--muted)}
.chip{display:inline-block;padding:1px 8px;border-radius:6px;font-size:11.5px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;
color:color-mix(in srgb,var(--c) 62%,var(--text));background:color-mix(in srgb,var(--c) 13%,transparent)}
.empty{padding:44px 22px;text-align:center;color:var(--muted)}
.empty svg{width:36px;height:36px;margin-bottom:8px;color:var(--border-strong)}
.empty p{margin:2px 0}
.empty code{display:inline-block;margin-top:10px;padding:6px 10px;border-radius:8px;background:var(--surface-2);border:1px solid var(--border);color:var(--text)}

.foot{display:flex;align-items:center;justify-content:center;gap:9px;margin-top:22px;font-size:12px;color:var(--muted)}
.foot .dot{--c:#12b76a}
.foot.err .dot{--c:#f04438}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
""".replace("%DARK%", _DARK_TOKENS)

# runs in <head> before first paint, so a saved theme never flashes the wrong colors
THEME_BOOT_JS = ("try{var t=localStorage.getItem('vp-theme');"
                 "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}")

DASH_JS = """
(function(){
var root=document.documentElement,K='vp-theme';
function cur(){var t=root.getAttribute('data-theme');return t==='light'||t==='dark'?t:'system'}
function mark(){var c=cur();document.querySelectorAll('[data-theme-set]').forEach(function(b){
b.setAttribute('aria-pressed',String(b.dataset.themeSet===c))})}
function legacyCopy(t,done){var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;opacity:0';
document.body.appendChild(a);a.select();try{if(document.execCommand('copy'))done()}catch(_){}a.remove()}
function copyText(t,done){
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done,function(){legacyCopy(t,done)})}
else{legacyCopy(t,done)}}
document.addEventListener('click',function(e){
var tb=e.target.closest('[data-theme-set]');
if(tb){var t=tb.dataset.themeSet;
if(t==='system')root.removeAttribute('data-theme');else root.setAttribute('data-theme',t);
try{if(t==='system')localStorage.removeItem(K);else localStorage.setItem(K,t)}catch(_){}
mark();return}
var b=e.target.closest('[data-copy]');if(!b)return;
copyText(b.dataset.copy,function(){var s=b.querySelector('span');s.textContent='Copied';b.classList.add('done');
setTimeout(function(){s.textContent='Copy';b.classList.remove('done')},1200)})});
var foot=document.getElementById('foot'),upd=document.getElementById('upd');
setInterval(async function(){try{
var r=await fetch('/?partial=1',{cache:'no-store'});if(!r.ok)throw 0;
document.getElementById('live').innerHTML=await r.text();
foot.classList.remove('err');upd.textContent='Live \\u00b7 updated '+new Date().toLocaleTimeString()
}catch(e){foot.classList.add('err');upd.textContent='Connection lost \\u00b7 retrying\\u2026'}},3000);
mark();
})();
"""


# --------------------------------------------------------------------------
# HTTP: IPP transport + status page + control
# --------------------------------------------------------------------------
class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VirtualPrinter/1.0"

    def log_message(self, *args):
        pass

    def _guard(self):
        st = STATE["value"]
        if st == "drop":
            self.close_connection = True
            return True
        if st == "hang":
            time.sleep(30)
            self.close_connection = True
            return True
        return False

    def _body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _send(self, code, body, ctype="text/html"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self._guard():
            return
        body = self._body()
        if "application/ipp" in self.headers.get("Content-Type", ""):
            host = self.headers.get("Host", "localhost")
            uri = f"ipp://{host}{self.path.split('?')[0]}"
            try:
                self._send(200, handle_ipp(body, uri), "application/ipp")
            except Exception as e:  # malformed request
                print(f"[ipp] bad request: {e}", flush=True)
                self._send(400, "bad IPP request", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_GET(self):
        # no hang/drop guard here: the dashboard must stay usable to switch the state back
        u = urlparse(self.path)
        if u.path == "/set":
            s = parse_qs(u.query).get("state", [""])[0]
            if s in STATES:
                STATE["value"] = s
                print(f"[state] -> {s}", flush=True)
                if "back" in parse_qs(u.query):  # dashboard buttons
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._send(200, f"state={s}\n", "text/plain")
            return self._send(400, f"state must be one of {STATES}\n", "text/plain")
        if u.path.startswith("/jobs/"):  # download a received job
            p = os.path.join(CFG["jobdir"], os.path.basename(u.path[6:]))
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "application/octet-stream")
            return self._send(404, "not found", "text/plain")
        self._send(200, self._dashboard(partial="partial" in parse_qs(u.query)))

    def _dashboard(self, partial=False):
        esc = htmllib.escape
        st = STATE["value"]
        host, source = display_host()
        port = dict(CFG["ports"])

        def size(n):
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024 or unit == "GB":
                    return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024

        def mtime(f):
            try:
                return time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(f)))
            except OSError:
                return "?"

        def copy_btn(text):
            return (f"<button type='button' class='btn' data-copy='{esc(text)}' title='Copy to clipboard'>"
                    f"{_icon('copy')}<span>Copy</span></button>")

        # --- connection endpoints (all built from the public/override host) ---
        endpoints = []
        if "HTTP" in port:
            endpoints.append(("Dashboard", f"http://{host}:{port['HTTP']}/"))
        if "raw/JetDirect" in port:
            endpoints.append(("Raw / JetDirect", f"socket://{host}:{port['raw/JetDirect']}"))
        if "LPD" in port:
            endpoints.append(("LPD", f"lpd://{host}:{port['LPD']}/queue"))
        if "IPP" in port:
            endpoints.append(("IPP", f"ipp://{host}:{port['IPP']}/ipp/print"))
        ep_html = "".join(
            "<div class='ep'>"
            f"<div><div class='label'>{esc(k)}</div><div class='ep-v mono'>{esc(v)}</div></div>{copy_btn(v)}</div>"
            for k, v in endpoints)

        if source == "lan":
            note = ("<div class='note warn'>Public IP could not be detected (no internet?). "
                    "Showing the LAN address. Use <code>--public-host</code> to set it manually.</div>")
        else:
            note = ("<div class='note'>Reachable from the internet only if your router or firewall "
                    "forwards these ports to this machine.</div>")

        src_label = {"public": "auto-detected", "manual": "manual", "lan": "LAN fallback"}[source]
        up = int(time.time() - CFG["started"])
        uptime = (f"{up // 86400}d " if up >= 86400 else "") + f"{up % 86400 // 3600}h {up % 3600 // 60}m {up % 60}s"
        total_bytes = sum(j[3] for j in JOBS)

        def stat(icon, label, value, sub=""):
            return (f"<div class='card stat'><div class='stat-ic'>{_icon(icon)}</div><div>"
                    f"<div class='label'>{label}</div><div class='val'>{value}</div>"
                    f"<div class='hint'>{sub}</div></div></div>")

        stats = (stat("globe", "Public IP" if source != "lan" else "Address", esc(host), src_label)
                 + stat("clock", "Uptime", uptime, f"since {time.strftime('%H:%M:%S', time.localtime(CFG['started']))}")
                 + stat("file", "Jobs received", str(len(JOBS)), "all protocols")
                 + stat("drive", "Data received", size(total_bytes), "total"))

        # --- state buttons ---
        btns = "".join(
            f"<a class='sbtn s-{s}' href='/set?state={s}&back=1'" + (" aria-current='true'" if s == st else "") + ">"
            f"<b><span class='dot'></span>{s}" + (_icon("check").replace("<svg ", "<svg class='chk' ", 1) if s == st else "")
            + f"</b><small>{STATE_INFO[s][1]}</small></a>"
            for s in STATES)

        # --- jobs table ---
        shown = list(reversed(JOBS[-100:]))
        rows = "".join(
            "<tr>"
            f"<td class='num muted'>#{j}</td>"
            f"<td class='num muted'>{mtime(f)}</td>"
            f"<td><span class='chip p-{p if p in ('raw', 'lpd', 'ipp') else 'x'}'>{esc(p)}</span></td>"
            f"<td class='muted mono'>{esc(os.path.splitext(f)[1][1:] or '-')}</td>"
            f"<td class='mono'><a href='/jobs/{esc(os.path.basename(f))}' download>{esc(os.path.basename(f))}</a></td>"
            f"<td class='r num'>{size(n)}</td></tr>"
            for j, p, f, n in shown)
        if rows:
            jobs_body = ("<div class='tw'><table><thead><tr><th>ID</th><th>Time</th><th>Protocol</th><th>Type</th>"
                         "<th>File</th><th class='r'>Size</th></tr></thead>"
                         f"<tbody>{rows}</tbody></table></div>")
        else:
            raw = port.get("raw/JetDirect")
            hint = f"<code>echo hello | nc {esc(host)} {raw}</code>" if raw else ""
            jobs_body = (f"<div class='empty'>{_icon('inbox')}<p><b>No jobs received yet</b></p>"
                         f"<p>Print to any of the endpoints above to see it appear here.</p>{hint}</div>")
        jobs_meta = f"{len(JOBS)} total &middot; newest first" + (" &middot; showing latest 100" if len(JOBS) > 100 else "")

        label = STATE_INFO[st][0]
        inner = (
            "<div class='hero'><div class='hero-l'>"
            f"<div class='hero-icon'>{_icon('printer')}</div>"
            f"<div><h1>{esc(CFG['name'])}</h1><p class='sub'>Virtual network printer &middot; live dashboard</p></div></div>"
            f"<span class='pill s-{st}'><span class='dot pulse'></span>{label}</span></div>"
            f"<div class='grid stats'>{stats}</div>"
            "<div class='grid cols'>"
            f"<section class='card panel'><h2>Connect to this printer</h2>{ep_html}{note}</section>"
            f"<section class='card panel'><h2>Simulate a state</h2><div class='states'>{btns}</div>"
            "<p class='help'><b>paperout / jam / offline</b> change the status replies. "
            "<b>hang</b> accepts connections and never answers; <b>drop</b> closes them immediately "
            "(raw, LPD and IPP).</p></section></div>"
            "<section class='card jobs'>"
            f"<div class='jobs-h'><h2>Received jobs</h2><span class='badge'>{jobs_meta}</span></div>"
            f"{jobs_body}</section>"
        )
        if partial:
            return inner

        theme = (
            "<div class='theme' role='group' aria-label='Color theme'>"
            f"<button type='button' data-theme-set='light' aria-pressed='false' title='Light' aria-label='Light theme'>{_icon('sun')}</button>"
            f"<button type='button' data-theme-set='system' aria-pressed='false' title='System' aria-label='Match system theme'>{_icon('monitor')}</button>"
            f"<button type='button' data-theme-set='dark' aria-pressed='false' title='Dark' aria-label='Dark theme'>{_icon('moon')}</button>"
            "</div>")
        title = APP_NAME if CFG["name"] == APP_NAME else f"{CFG['name']} \u00b7 {APP_NAME}"
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='light dark'>"
            f"<title>{esc(title)}</title><link rel='icon' href=\"{FAVICON}\">"
            f"<script>{THEME_BOOT_JS}</script><style>{DASH_CSS}</style></head><body>"
            f"<header class='topbar'><div class='topbar-in'><div class='brand'><div class='logo'>{_icon('printer')}</div>"
            f"{APP_NAME}</div>{theme}</div></header>"
            f"<main class='wrap'><div id='live'>{inner}</div>"
            "<div class='foot' id='foot'><span class='dot pulse'></span>"
            "<span id='upd'>Live &middot; refreshes every 3 seconds</span></div></main>"
            f"<script>{DASH_JS}</script></body></html>"
        )


# --------------------------------------------------------------------------
class TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--offset", type=int, default=0, help="add to every port (e.g. 10000 to avoid root)")
    ap.add_argument("--jobs", default="jobs", help="directory for received jobs")
    ap.add_argument("--state", default="ok", choices=STATES)
    ap.add_argument("--name", default=CFG["name"])
    ap.add_argument("--public-host", help="IP or hostname to show on the dashboard (skips auto-detection)")
    ap.add_argument("--no-public-lookup", action="store_true", help="never contact external services to find the public IP")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser on start")
    ap.add_argument("--idle", type=float, default=2.0, help="seconds of silence that end a raw job")
    args = ap.parse_args()
    CFG.update(jobdir=args.jobs, name=args.name, idle=args.idle)
    STATE["value"] = args.state
    IP["lan"] = lan_ip()
    IP["override"] = args.public_host
    if not args.public_host and not args.no_public_lookup:
        IP["public"] = fetch_public_ip()  # first lookup up front so the startup banner is accurate
        threading.Thread(target=public_ip_loop, daemon=True).start()

    services = [
        ("raw/JetDirect", 9100, lambda p: TCP((args.host, p), Raw)),
        ("LPD", 515, lambda p: TCP((args.host, p), LPD)),
        ("IPP", 631, lambda p: ThreadingHTTPServer((args.host, p), Web)),
        ("HTTP", 80, lambda p: ThreadingHTTPServer((args.host, p), Web)),
    ]
    for name, port, make in services:
        p = port + args.offset
        try:
            srv = make(p)
        except PermissionError:
            print(f"! {name}: port {p} needs root. Re-run with sudo or --offset 10000", file=sys.stderr)
            continue
        except OSError as e:
            print(f"! {name}: port {p}: {e}", file=sys.stderr)
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        CFG["ports"].append((name, p))
        print(f"  {name:14} listening on {args.host}:{p}")
    print(f"State: {STATE['value']}   Jobs saved to: {os.path.abspath(CFG['jobdir'])}")
    web = dict(CFG["ports"]).get("HTTP")
    if web:
        print(f"Dashboard: http://localhost:{web}/")
        shown, source = display_host()
        print(f"Shown on dashboard ({source}): http://{shown}:{web}/")
        if args.open:
            webbrowser.open(f"http://localhost:{web}/")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()