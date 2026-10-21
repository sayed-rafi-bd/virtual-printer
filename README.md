# Virtual Printer

One Python script (`virtualprinter.py`) plus a `web/` folder with the dashboard's templates and
styles, and a setup script for the pieces that need system services. The printer itself has no
dependencies; only the web dashboard needs [Jinja2](https://jinja.palletsprojects.com/).

## Quick start

```bash
pip install -r requirements.txt          # Jinja2, for the dashboard only
python3 virtualprinter.py                   # high ports, no root: 19100, 10515, 10631, 10080
sudo python3 virtualprinter.py --offset 0   # standard ports: 9100, 515, 631, 80
```

Received jobs are saved in `./jobs/` (file type sniffed: pdf, ps, pjl, zpl, escpos, ...).

## Dashboard

The server prints `Dashboard: http://localhost:<port>/` on start (port 10080 by default, or 80 with
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
| Raw / JetDirect (AppSocket) | 19100 | `virtualprinter.py` |
| LPD / LPR | 10515 | `virtualprinter.py` |
| IPP (HTTP transport) | 10631 | `virtualprinter.py` (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs) |
| HTTP embedded page | 10080 | `virtualprinter.py` |
| mDNS / Bonjour / AirPrint discovery | 5353/udp | `setup.sh avahi` |
| SMB / Windows printer sharing | 445 | `setup.sh cups samba` (SMB -> CUPS -> raw 19100 -> Virtual Printer) |
| SNMP Printer-MIB | 10161/udp | `setup.sh snmp` + `public.snmprec` |
| USB | - | `setup.sh usb` (kernel `dummy_hcd` + `g_printer`) |

Ports above are the defaults (`--offset 10000`); use `--offset 0` for the standard 9100/515/631/80.

Not included: WS-Discovery/WSD (3702), IPPS/TLS, FTP printing, Bluetooth.

## Printer languages answered on the raw port

- **PJL**: `@PJL INFO STATUS / ID / PAGECOUNT / CONFIG`, `USTATUS`, `ECHO`
- **ESC/POS**: real-time status `DLE EOT n` (n = 1..4)
- **ZPL**: `~HS` host status, `~HI` identification, `~HQES`

Status codes are representative, not copied from a specific model. Adjust the tables at the
top of `virtualprinter.py` to match the device you are emulating.

## Error injection

```bash
curl 'http://localhost:10080/set?state=paperout'   # ok | paperout | jam | offline | hang | drop
```

`hang` accepts the connection and never answers (timeout tests); `drop` closes immediately.
Start in a state with `--state jam`.

## Test commands

```bash
# raw job
printf 'hello' | nc -q1 localhost 19100
# PJL status
printf '\033%%-12345X@PJL INFO STATUS\r\n\033%%-12345X' | nc -q2 localhost 19100 | cat -v
# ESC/POS paper sensor query
printf '\x10\x04\x04' | nc -q1 localhost 19100 | xxd
# ZPL status
printf '~HS' | nc -q1 localhost 19100
# LPD
lpr -H localhost:10515 -P anyqueue somefile.txt
# IPP
ipptool -tv ipp://localhost:10631/ipp/print get-printer-attributes.test
ipptool -tv -f somefile.pdf ipp://localhost:10631/ipp/print print-job.test
# SNMP (after setup.sh snmp)
snmpwalk -v2c -c public localhost:10161 1.3.6.1.2.1.43
```

## Docker

```bash
docker compose up --build              # printer (19100, 10515, 10631) + SNMP (10161/udp)
docker compose --profile multi up      # also a second printer on ports 29100/20515/20631
```

- Dashboard: <http://localhost:10080> (printer 1), <http://localhost:20080> (printer 2).
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