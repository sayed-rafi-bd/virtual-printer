FROM python:3.12-slim

WORKDIR /app

# Jinja2 renders the web dashboard (the printer itself has no dependencies)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY virtualprinter.py .
COPY web/ web/
RUN mkdir /jobs

# raw/JetDirect, LPD, IPP, dashboard
EXPOSE 20100 11515 11631 11080
VOLUME /jobs

HEALTHCHECK --interval=15s --timeout=3s --start-period=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11080/', timeout=2)" || exit 1

# extra flags go in `command:` / after the image name, e.g. --name "Receipt Printer" --state jam
ENTRYPOINT ["python", "-u", "virtualprinter.py", "--jobs", "/jobs"]
