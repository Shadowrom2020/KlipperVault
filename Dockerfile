FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KLIPPERVAULT_AUTO_UPDATE_VENV=0 \
    KLIPPERVAULT_RUNTIME_MODE=off_printer \
    KLIPPERVAULT_CONFIG_DIR=/data/config \
    KLIPPERVAULT_DB_PATH=/data/db/klipper_macros.db

WORKDIR /app

RUN useradd --create-home --home-dir /home/klippervault --shell /usr/sbin/nologin klippervault

COPY requirements.txt requirements-printer.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/config /data/db \
    && chown -R klippervault:klippervault /app /data

USER klippervault

EXPOSE 10090

CMD ["python3", "klipper_vault_gui.py"]
