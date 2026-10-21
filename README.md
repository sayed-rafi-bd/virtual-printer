# virtualprinter (Rust)

Rust port of `virtualprinter.py`: a virtual network printer that accepts and stores print jobs,
answers status queries, and ships a small live web dashboard. No dependencies (std only);
the dashboard is a single HTML file embedded in the binary.

| Port (default, offset 11000) | Protocol |
|---|---|
| 20100 | Raw / JetDirect (+ PJL, ESC/POS, ZPL status replies) |
| 11515 | LPD / LPR |
| 11631 | IPP (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, Cancel-Job, Get-Job-Attributes) |
| 11080 | HTTP: dashboard `/`, JSON `/api/status`, `/set?state=jam`, `/jobs/<file>` |

## Docker

    docker compose up --build        # dashboard: http://localhost:8080/
    # printers:  socket://localhost:9100   lpd://localhost:515/queue   ipp://localhost:631/ipp/print

Received jobs are kept in the `jobs` volume (`/jobs` in the container).
Without compose:

    docker build -t virtualprinter .
    docker run --rm -p 9100:9100 -p 515:515 -p 631:631 -p 8080:80 -v vp-jobs:/jobs virtualprinter

Notes
- The image runs as a non-root user and listens on the standard ports inside the container.
  Docker allows that by default; if your runtime does not (e.g. rootless Podman), either add
  `--sysctl net.ipv4.ip_unprivileged_port_start=0`, or fall back to high ports:
  `docker run -p 9100:20100 -p 515:11515 -p 631:11631 -p 8080:11080 virtualprinter --offset 11000`
  (the dashboard then shows the container's ports, i.e. 20100/11515/11631).
- Extra options are passed after the image name, e.g. `virtualprinter --name "Front Desk" --state jam`.
- If the host already runs CUPS on 631, change the left side of that mapping.

## Build & run without Docker

    cargo build --release
    ./target/release/virtualprinter                 # high ports, no root; dashboard http://localhost:11080/
    sudo ./target/release/virtualprinter --offset 0 # standard 9100 / 515 / 631 / 80

Options: `--host`, `--offset`, `--jobs DIR`, `--state`, `--name`, `--idle SECS` (see `--help`).

## States

Switch from the dashboard or with `curl 'http://localhost:11080/set?state=jam'`:
`ok`, `paperout`, `jam`, `offline`, `hang` (accept, never answer), `drop` (close immediately).

## Differences from the Python version
- The dashboard is a static page + JSON API (`web/index.html`) instead of Jinja2 templates;
  there is no `--web`, `--open`, `--public-host` or `--no-public-lookup`. The "Connect" addresses
  use the host name you opened the dashboard with (no public-IP lookup).
- Job file names and the jobs table use UTC (Python used local time).
