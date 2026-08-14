FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin printeragent

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY main.py ./main.py
COPY docs ./docs
COPY systemd ./systemd

RUN python -m pip install .

USER printeragent

CMD ["python", "-m", "printer_agent", "--config", "/etc/printer-agent/agent.yaml", "run"]
