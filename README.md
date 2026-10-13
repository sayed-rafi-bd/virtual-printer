# Fake printer toolkit

One Python script (`fakeprinter.py`, no dependencies) plus a setup script for the pieces
that need system services.

## Quick start

```bash
sudo python3 fakeprinter.py              # real ports: 9100, 515, 631, 80
python3 fakeprinter.py --offset 10000    # no root: 19100, 10515, 10631, 10080
```

Received jobs are saved in `./jobs/` (file type sniffed: pdf, ps, pjl, zpl, escpos, ...).

## Dashboard

The server prints `Dashboard: http://localhost:<port>/` on start (port 80, or 10080 with
`--offset 10000`); add `--open` to launch it in your browser automatically. The page shows
the printer details (name, state, uptime, connection URIs for raw/LPD/IPP), buttons to switch
the error state, and a live job list (newest first, auto-refresh every 3s) where each job can
be downloaded.

The page is styled with Tailwind CSS loaded from its CDN, so the browser needs internet access
to get the styling. Offline it still works, just unstyled.

## Protocol coverage

| Protocol | Port | Provided by |
|---|---|---|
| Raw / JetDirect (AppSocket) | 9100 | `fakeprinter.py` |
| LPD / LPR | 515 | `fakeprinter.py` |
| IPP (HTTP transport) | 631 | `fakeprinter.py` (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs) |
| HTTP embedded page | 80 | `fakeprinter.py` |
| mDNS / Bonjour / AirPrint discovery | 5353/udp | `setup.sh avahi` |
| SMB / Windows printer sharing | 445 | `setup.sh cups samba` (SMB -> CUPS -> raw 9100 -> fakeprinter) |
| SNMP Printer-MIB | 161/udp | `setup.sh snmp` + `public.snmprec` |
| USB | - | `setup.sh usb` (kernel `dummy_hcd` + `g_printer`) |

Not included: WS-Discovery/WSD (3702), IPPS/TLS, FTP printing, Bluetooth.

## Printer languages answered on the raw port

- **PJL**: `@PJL INFO STATUS / ID / PAGECOUNT / CONFIG`, `USTATUS`, `ECHO`
- **ESC/POS**: real-time status `DLE EOT n` (n = 1..4)
- **ZPL**: `~HS` host status, `~HI` identification, `~HQES`

Status codes are representative, not copied from a specific model. Adjust the tables at the
top of `fakeprinter.py` to match the device you are emulating.

## Error injection

```bash
curl 'http://localhost/set?state=paperout'   # ok | paperout | jam | offline | hang | drop
```

`hang` accepts the connection and never answers (timeout tests); `drop` closes immediately.
Start in a state with `--state jam`.

## Test commands

```bash
# raw job
printf 'hello' | nc -q1 localhost 9100
# PJL status
printf '\033%%-12345X@PJL INFO STATUS\r\n\033%%-12345X' | nc -q2 localhost 9100 | cat -v
# ESC/POS paper sensor query
printf '\x10\x04\x04' | nc -q1 localhost 9100 | xxd
# ZPL status
printf '~HS' | nc -q1 localhost 9100
# LPD
lpr -H localhost:515 -P anyqueue somefile.txt
# IPP
ipptool -tv ipp://localhost:631/ipp/print get-printer-attributes.test
ipptool -tv -f somefile.pdf ipp://localhost:631/ipp/print print-job.test
# SNMP (after setup.sh snmp)
snmpwalk -v2c -c public localhost 1.3.6.1.2.1.43
```

## Docker

```bash
docker compose up --build              # printer (9100, 515, 631) + SNMP (161/udp)
docker compose --profile multi up      # also a second printer on ports 19100/10515/10631
```

- Dashboard: <http://localhost:8080> (printer 1), <http://localhost:10080> (printer 2).
- Jobs are written to `./jobs` (and `./jobs2`) on the host through a bind mount.
- Pass flags through `command:` in `docker-compose.yml` (`--name`, `--state`, `--offset`).
- mDNS discovery, Samba and the USB gadget are host-level features. Use `setup.sh`
  for those, or run the printer container with `network_mode: host` on Linux.

## Multiple printers

Run several instances with different `--offset`, `--name`, `--jobs` and `--state`, or give
each its own IP (extra addresses on an interface, or network namespaces / containers).
