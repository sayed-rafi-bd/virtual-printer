#!/usr/bin/env python3
"""
virtualprinter.py - Virtual Printer: a multi-protocol virtual network printer (Python 3.8+; the printer is stdlib-only, the web dashboard needs Jinja2).

Listens on (default --offset 11000, so no root is needed):
  20100  Raw / JetDirect  (also answers PJL, ESC/POS and ZPL status queries)
  11515  LPD / LPR
  11631  IPP  (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, ...)
  11080  HTTP status page + state control  (GET /set?state=jam)

Every received job is saved under ./jobs/.  The dashboard is rendered with Jinja2 from ./web
(see README: "Customizing the dashboard").

Run:   python3 virtualprinter.py                 (high ports: 20100, 11515, 11631, 11080)
       sudo python3 virtualprinter.py --offset 0    (standard ports: 9100, 515, 631, 80)

States (change at runtime with  curl 'http://localhost:11080/set?state=jam'):
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
import mimetypes
import webbrowser
import os
import re
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

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
# web dashboard: Jinja2 templates in ./web/templates, static files in ./web/static
# (copy ./web and pass --web DIR to customize; the printer itself needs no dependencies)
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

WEB = {"dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"), "env": None}

NO_JINJA_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>Virtual Printer</title>"
    "<body style='font:15px/1.6 system-ui,sans-serif;max-width:640px;margin:15vh auto;padding:0 24px'>"
    "<h2>Virtual Printer is running</h2>"
    "<p>The web dashboard needs <b>Jinja2</b>, which is not available. The printer itself "
    "(raw, LPD, IPP) works normally, and <code>/set?state=...</code> still switches states.</p>"
    "<p>Install it and restart: <code>pip install -r requirements.txt</code></p>")


def init_web(web_dir):
    """Set up the template engine. Only the dashboard needs Jinja2; the printer works without it."""
    WEB["dir"] = os.path.abspath(web_dir)
    tpl_dir = os.path.join(WEB["dir"], "templates")
    try:
        import jinja2
    except ImportError:
        print("! dashboard disabled: Jinja2 is not installed (pip install -r requirements.txt)", file=sys.stderr)
        return
    if not os.path.isdir(tpl_dir):
        print(f"! dashboard disabled: no templates folder at {tpl_dir}", file=sys.stderr)
        return
    WEB["env"] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(tpl_dir),
        autoescape=True,      # every {{ variable }} is HTML-escaped (printer name, job file names, ...)
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=True,     # edit a template and refresh the browser, no restart needed
    )


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_mtime(path):
    try:
        return time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return "?"


def dashboard_context():
    """Everything the templates can use. Documented at the top of web/templates/_live.html."""
    st = STATE["value"]
    host, source = display_host()
    port = dict(CFG["ports"])

    endpoints = []
    if "HTTP" in port:
        endpoints.append({"label": "Dashboard", "url": f"http://{host}:{port['HTTP']}/"})
    if "raw/JetDirect" in port:
        endpoints.append({"label": "Raw / JetDirect", "url": f"socket://{host}:{port['raw/JetDirect']}"})
    if "LPD" in port:
        endpoints.append({"label": "LPD", "url": f"lpd://{host}:{port['LPD']}/queue"})
    if "IPP" in port:
        endpoints.append({"label": "IPP", "url": f"ipp://{host}:{port['IPP']}/ipp/print"})

    up = int(time.time() - CFG["started"])
    all_jobs = list(JOBS)
    return {
        "app_name": APP_NAME,
        "printer_name": CFG["name"],
        "title": APP_NAME if CFG["name"] == APP_NAME else f"{CFG['name']} \u00b7 {APP_NAME}",
        "state": st,
        "state_label": STATE_INFO[st][0],
        "states": [{"name": n, "description": STATE_INFO[n][1], "active": n == st} for n in STATES],
        "host": host,
        "source": source,
        "source_label": {"public": "auto-detected", "manual": "manual", "lan": "LAN fallback"}[source],
        "uptime": (f"{up // 86400}d " if up >= 86400 else "") + f"{up % 86400 // 3600}h {up % 3600 // 60}m {up % 60}s",
        "started": time.strftime("%H:%M:%S", time.localtime(CFG["started"])),
        "endpoints": endpoints,
        "raw_port": port.get("raw/JetDirect"),
        "jobs_count": len(all_jobs),
        "jobs_size": fmt_size(sum(j[3] for j in all_jobs)),
        "jobs": [
            {"id": j, "time": fmt_mtime(f), "proto": p, "ext": os.path.splitext(f)[1][1:] or "-",
             "filename": os.path.basename(f), "size": fmt_size(n)}
            for j, p, f, n in reversed(all_jobs[-100:])
        ],
    }


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

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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
        if u.path.startswith("/static/"):  # dashboard css / js / icons
            return self._static(unquote(u.path[len("/static/"):]))
        code, body = self._dashboard(partial="partial" in parse_qs(u.query))
        self._send(code, body)

    def _dashboard(self, partial=False):
        """Render a template. Returns (http_status, html)."""
        env = WEB["env"]
        if env is None:
            return 503, NO_JINJA_PAGE
        try:
            tpl = env.get_template("_live.html" if partial else "dashboard.html")
            return 200, tpl.render(**dashboard_context())
        except Exception as e:  # a mistake while customizing: show it in the browser, keep the server alive
            traceback.print_exc()
            where = f" ({e.name}, line {e.lineno})" if hasattr(e, "lineno") and hasattr(e, "name") else ""
            msg = htmllib.escape(f"{type(e).__name__}: {e}{where}")
            return 500, ("<!doctype html><meta charset='utf-8'><title>Template error</title>"
                         "<body style='font:15px/1.6 system-ui,sans-serif;max-width:760px;margin:10vh auto;padding:0 24px'>"
                         f"<h2>Dashboard template error</h2><pre style='white-space:pre-wrap'>{msg}</pre>"
                         "<p>Fix the template and refresh the page. The full traceback is in the server log.</p>")

    def _static(self, rel):
        root = os.path.realpath(os.path.join(WEB["dir"], "static"))
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):  # also blocks ../ traversal
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype, {"Cache-Control": "no-cache"})

# --------------------------------------------------------------------------
class TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--offset", type=int, default=11000, help="add to every port (default 11000 = high ports, no root; use 0 for the standard 9100/515/631/80)")
    ap.add_argument("--jobs", default="jobs", help="directory for received jobs")
    ap.add_argument("--state", default="ok", choices=STATES)
    ap.add_argument("--name", default=CFG["name"])
    ap.add_argument("--public-host", help="IP or hostname to show on the dashboard (skips auto-detection)")
    ap.add_argument("--no-public-lookup", action="store_true", help="never contact external services to find the public IP")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser on start")
    ap.add_argument("--web", default=WEB["dir"], help="folder with templates/ and static/ for the dashboard (copy ./web to customize it)")
    ap.add_argument("--idle", type=float, default=2.0, help="seconds of silence that end a raw job")
    args = ap.parse_args()
    CFG.update(jobdir=args.jobs, name=args.name, idle=args.idle)
    init_web(args.web)
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
            print(f"! {name}: port {p} needs root. Re-run with sudo or use a higher --offset", file=sys.stderr)
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