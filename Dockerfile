# ---- build ---------------------------------------------------------------
# Pin the Debian release on both stages so the binary's glibc matches the runtime image.
FROM rust:1-slim-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY web ./web
RUN cargo build --release --locked

# ---- runtime -------------------------------------------------------------
FROM debian:bookworm-slim
RUN useradd --system --uid 10001 --no-create-home printer \
 && mkdir /jobs && chown printer /jobs
COPY --from=build /src/target/release/virtualprinter /usr/local/bin/virtualprinter

USER printer
VOLUME /jobs
# raw/JetDirect, LPD, IPP, HTTP dashboard (standard ports; binding <1024 as non-root needs
# net.ipv4.ip_unprivileged_port_start=0, which Docker sets by default and docker-compose.yml sets explicitly)
EXPOSE 9100 515 631 80

ENTRYPOINT ["virtualprinter", "--offset", "0", "--jobs", "/jobs"]
CMD []
