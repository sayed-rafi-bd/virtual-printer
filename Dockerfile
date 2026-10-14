FROM python:3.12-slim

WORKDIR /app
COPY virtualprinter.py .
RUN mkdir /jobs

# raw/JetDirect, LPD, IPP, dashboard
EXPOSE 9100 515 631 80
VOLUME /jobs

HEALTHCHECK --interval=15s --timeout=3s --start-period=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:80/', timeout=2)" || exit 1

# extra flags go in `command:` / after the image name, e.g. --name "Receipt Printer" --state jam
ENTRYPOINT ["python", "-u", "virtualprinter.py", "--jobs", "/jobs"]
