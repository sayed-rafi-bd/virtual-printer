# Virtual Printer

One Python script (`virtualprinter.py`) plus a `web/` folder with the dashboard's templates and
styles, and a setup script for the pieces that need system services. The printer itself has no
dependencies; only the web dashboard needs [Jinja2](https://jinja.palletsprojects.com/).

## Quick start

```bash
pip install -r requirements.txt          # Jinja2, for the dashboard only
python3 virtualprinter.py                   # high ports, no root: 20100, 11515, 11631, 11080
sudo python3 virtualprinter.py --offset 0   # standard ports: 9100, 515, 631, 80
```

Received jobs are saved in `./jobs/` (file type sniffed: pdf, ps, pjl, zpl, escpos, ...).

On Windows, see [Add the printer in Windows (PowerShell)](#add-the-printer-in-windows-powershell) to install it as a real printer queue.

## Dashboard

The server prints `Dashboard: http://localhost:<port>/` on start (port 11080 by default, or 80 with
`--offset 0`); add `--open` to launch it in your browser automatically. The page shows
live stats (address, uptime, jobs, data received), copyable connection URIs for raw/LPD/IPP,
buttons to switch the error state, and a live job list (newest first, auto-refresh every 3s)
where each job can be downloaded. It has a **light and dark theme** with a switcher next to the status badge
(Light / System / Dark); the choice is remembered in your browser, and "System" follows your OS setting.

### Public IP instead of localhost

The connection URIs on the dashboard use your **public IP**, not `localhost`. It is detected
at startup (ipify.org, with ifconfig.me and icanhazip.com as fallbacks) and re-checked every
10 minutes. If detection fails (offline), the LAN address is shown instead, with a notice.

```bash
python3 virtualprinter.py                              # auto-detect public IP
python3 virtualprinter.py --public-host printer.example.com   # show a fixed IP / hostname
python3 virtualprinter.py --no-public-lookup           # never contact external services
```

Use `--public-host` behind NAT/Docker setups where the detected IP is not the one clients
should use. The IP is only *displayed*: the printer is reachable from the internet only if
your router/firewall forwards the ports. The dashboard and `/set` endpoint have no
authentication, so do not expose the dashboard port publicly without restricting access.

The dashboard needs no internet (no CDN): the CSS, JS and icons are served from `web/static/`.
Styling is [Tailwind CSS](https://tailwindcss.com) v4, precompiled into `web/static/tailwind.css`.
Without Jinja2 installed the printer still works; the dashboard shows a page telling you to
install it.

## Customizing the dashboard

The page is a set of Jinja2 templates plus plain CSS and JS, all in `web/`:

```
web/
  templates/
    base.html        page shell: <head>, footer, scripts
    dashboard.html   the page (wraps the live area)
    _live.html       hero (with the theme switcher), stats, connect panel, state buttons, jobs table
    _icons.html      inline SVG icon macro: {{ icon("printer") }}
  static/
    dashboard.css    colors are CSS variables at the top (light + dark)
    tailwind.css     compiled Tailwind output (generated, do not edit by hand)
    dashboard.js     theme switcher, copy buttons, 3-second live refresh
    favicon.svg
```

To make your own version, copy the folder and point the server at it:

```bash
cp -r web my-web
python3 virtualprinter.py --web my-web
```

Templates are re-read on every request and static files are never cached, so **edit, save, refresh**:
no restart. If a template has an error, the browser shows the message (with file and line) and the
server keeps running.

- **Re-color / re-brand:** the styling is Tailwind utility classes in the templates (e.g. swap
  `indigo-600` for `emerald-600`, and the matching `dark:` classes). Status colors are in the `tone`
  dict at the top of `_live.html`. After changing classes, rebuild the CSS (see below).
- **Change the layout:** edit `_live.html`. The variables it can use are listed at the top of that
  file (`state`, `endpoints`, `jobs`, `host`, ...). All values are HTML-escaped automatically.
- **Add data:** add a key to `dashboard_context()` in `virtualprinter.py`, then use it as `{{ key }}`.
- **Docker:** uncomment `./web:/app/web:ro` in `docker-compose.yml` to mount your folder.

### Styling with Tailwind

The compiled `web/static/tailwind.css` is committed, so running or deploying the printer needs no Node.
Only when you add or change utility classes in `web/templates/` or `dashboard.js`:

```bash
npm install          # once
npm run build:css    # or: npm run watch:css while editing
```

`tailwind.input.css` is the only source file (it scans the templates and sets up the `data-theme`
dark-mode variant). Write full class names in templates; Tailwind cannot see names built by string concatenation.

## Protocol coverage

| Protocol | Port | Provided by |
|---|---|---|
| Raw / JetDirect (AppSocket) | 20100 | `virtualprinter.py` |
| LPD / LPR | 11515 | `virtualprinter.py` |
| IPP (HTTP transport) | 11631 | `virtualprinter.py` (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs) |
| HTTP embedded page | 11080 | `virtualprinter.py` |
| mDNS / Bonjour / AirPrint discovery | 5353/udp | `setup.sh avahi` |
| SMB / Windows printer sharing | 445 | `setup.sh cups samba` (SMB -> CUPS -> raw 20100 -> Virtual Printer) |
| SNMP Printer-MIB | 11161/udp | `setup.sh snmp` + `public.snmprec` |
| USB | - | `setup.sh usb` (kernel `dummy_hcd` + `g_printer`) |

Ports above are the defaults (`--offset 11000`); use `--offset 0` for the standard 9100/515/631/80.

Not included: WS-Discovery/WSD (3702), IPPS/TLS, FTP printing, Bluetooth.

## Printer languages answered on the raw port

- **PJL**: `@PJL INFO STATUS / ID / PAGECOUNT / CONFIG`, `USTATUS`, `ECHO`
- **ESC/POS**: real-time status `DLE EOT n` (n = 1..4)
- **ZPL**: `~HS` host status, `~HI` identification, `~HQES`

Status codes are representative, not copied from a specific model. Adjust the tables at the
top of `virtualprinter.py` to match the device you are emulating.

## Error injection

```bash
curl 'http://localhost:11080/set?state=paperout'   # ok | paperout | jam | offline | hang | drop
```

`hang` accepts the connection and never answers (timeout tests); `drop` closes immediately.
Start in a state with `--state jam`.

## Test commands

```bash
# raw job
printf 'hello' | nc -q1 localhost 20100
# PJL status
printf '\033%%-12345X@PJL INFO STATUS\r\n\033%%-12345X' | nc -q2 localhost 20100 | cat -v
# ESC/POS paper sensor query
printf '\x10\x04\x04' | nc -q1 localhost 20100 | xxd
# ZPL status
printf '~HS' | nc -q1 localhost 20100
# LPD
lpr -H localhost:11515 -P anyqueue somefile.txt
# IPP
ipptool -tv ipp://localhost:11631/ipp/print get-printer-attributes.test
ipptool -tv -f somefile.pdf ipp://localhost:11631/ipp/print print-job.test
# SNMP (after setup.sh snmp)
snmpwalk -v2c -c public localhost:11161 1.3.6.1.2.1.43
```

## Add the printer in Windows (PowerShell)

Windows can print to the Virtual Printer through a Standard TCP/IP port (raw / JetDirect), which
is the most reliable method. Everything below uses the built-in `PrintManagement` cmdlets, so
nothing extra needs to be installed.

### 1. Start the printer

On the machine that will run it (Windows, Linux or Docker):

```powershell
pip install -r requirements.txt
python virtualprinter.py                 # high ports, no admin: raw 20100, LPD 11515, IPP 11631, dashboard 11080
python virtualprinter.py --offset 0      # standard ports 9100/515/631/80 (run PowerShell as Administrator)
```

Or with Docker: `docker compose up --build`. Check <http://localhost:11080> loads before continuing.
If the printer runs on another machine, use its LAN IP instead of `127.0.0.1` below, and allow
the port through that machine's firewall.

### 2. Add it as a raw TCP/IP printer

Open **PowerShell as Administrator** and run:

```powershell
# --- settings: change these to match how you started the printer ---
$PrinterName = "Virtual Printer"
$HostAddress = "127.0.0.1"     # IP of the machine running virtualprinter.py
$RawPort     = 20100           # 20100 by default, 9100 with --offset 0
$PortName    = "VP_${HostAddress}_$RawPort"

# 1) create the TCP/IP port
if (-not (Get-PrinterPort -Name $PortName -ErrorAction SilentlyContinue)) {
    Add-PrinterPort -Name $PortName -PrinterHostAddress $HostAddress -PortNumber $RawPort
}

# 2) create the printer with a driver built into Windows
#    "Generic / Text Only" is the simplest: it sends plain text, so jobs are easy to read
Add-Printer -Name $PrinterName -DriverName "Generic / Text Only" -PortName $PortName

# 3) confirm
Get-Printer -Name $PrinterName | Format-List Name, DriverName, PortName, PrinterStatus
```

To get PDF-style or PostScript output instead of plain text, use a different built-in driver:

```powershell
Get-PrinterDriver | Select-Object Name        # list the drivers available on this PC
Add-Printer -Name "Virtual Printer (PS)" -DriverName "Microsoft PS Class Driver" -PortName $PortName
```

### 3. Send a test print

```powershell
"Hello from PowerShell" | Out-Printer -Name "Virtual Printer"

# or print a file
Get-Content .\somefile.txt | Out-Printer -Name "Virtual Printer"
```

The job appears in `./jobs/` and on the dashboard (<http://localhost:11080>) within a few seconds.
To check what Windows thinks of the queue: `Get-PrintJob -PrinterName "Virtual Printer"`.

### Alternative: add it over IPP

IPP needs no manual port or driver choice (Windows uses its built-in IPP class driver). The
URL must match the port the server is listening on:

```powershell
Add-Printer -ConnectionName "http://127.0.0.1:11631/ipp/print"
```

Use port `631` instead of `11631` if you started the server with `--offset 0`. If this fails with
an "operation could not be completed" error, enable the Windows IPP client first (see below).

### Alternative: add it over LPD / LPR

Windows' LPR port monitor only talks to the standard port 515, so this works only with
`--offset 0`. Enable the feature once, then add the port:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Printing-LPRPortMonitor -NoRestart   # Admin; may need a reboot

Add-PrinterPort -Name "VP_LPR" -LprHostAddress "127.0.0.1" -LprQueueName "queue" -LprByteCounting
Add-Printer -Name "Virtual Printer (LPR)" -DriverName "Generic / Text Only" -PortName "VP_LPR"
```

### Remove the printer

```powershell
Remove-Printer     -Name "Virtual Printer"
Remove-PrinterPort -Name "VP_127.0.0.1_20100"
```

### Troubleshooting

| Problem | Fix |
|---|---|
| `Add-Printer : The specified driver does not exist` | Run `Get-PrinterDriver` and use one of the listed names, or install the driver with `Add-PrinterDriver -Name "Generic / Text Only"`. |
| `Add-PrinterPort` / `Add-Printer` says access denied | Re-open PowerShell with **Run as administrator**. |
| Jobs stay in the queue or the printer shows *Offline* | Confirm the server is running and the port is right: `Test-NetConnection 127.0.0.1 -Port 20100`. Also check the dashboard error state is **ok** (not `offline`, `hang` or `drop`). |
| Works locally but not from another PC | Allow the port through the Windows/Linux firewall on the server machine, e.g. `New-NetFirewallRule -DisplayName "Virtual Printer raw" -Direction Inbound -Protocol TCP -LocalPort 20100 -Action Allow`. |
| Windows keeps sending to the wrong port | Remove the old port first (`Remove-PrinterPort`), then recreate it; an existing port keeps its old settings. |
| IPP: `Add-Printer -ConnectionName` fails | Enable the client: `Enable-WindowsOptionalFeature -Online -FeatureName Printing-Foundation-InternetPrinting-Client -NoRestart`, then retry. |
| `python` is not recognized | Install Python from python.org (tick *Add to PATH*), or use `py virtualprinter.py`. |

To test the error states from Windows (paper out, jam, ...), see [Error injection](#error-injection);
`Invoke-WebRequest "http://localhost:11080/set?state=paperout"` does the same as `curl`.

## Docker

```bash
docker compose up --build              # printer (20100, 11515, 11631) + SNMP (11161/udp)
docker compose --profile multi up      # also a second printer on ports 30100/21515/21631
```

- Dashboard: <http://localhost:11080> (printer 1), <http://localhost:21080> (printer 2).
- Jobs are stored in Docker named volumes (`printer_jobs`, `printer2_jobs`), so they survive
  `docker compose down` and rebuilds. `docker compose down -v` deletes them. To get files out:
  download them from the dashboard, or run
  `docker cp virtualprinter:/jobs ./jobs-backup`, or
  `docker run --rm -v virtual-printer_printer_jobs:/jobs -v "$PWD":/out alpine cp -r /jobs /out`
  (the volume name is prefixed with the compose project name, see `docker volume ls`).
  To use a host folder again, replace `printer_jobs:/jobs` with `./jobs:/jobs`.
- Pass flags through `command:` in `docker-compose.yml` (`--name`, `--state`, `--offset`).
- mDNS discovery, Samba and the USB gadget are host-level features. Use `setup.sh`
  for those, or run the printer container with `network_mode: host` on Linux.

## Multiple printers

Run several instances with different `--offset`, `--name`, `--jobs` and `--state`, or give
each its own IP (extra addresses on an interface, or network namespaces / containers).
