FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KLIPPERVAULT_AUTO_UPDATE_VENV=0

WORKDIR /app

RUN useradd --create-home --home-dir /home/klippervault --shell /usr/sbin/nologin klippervault

COPY requirements.txt requirements-printer.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /home/klippervault/.config/klippervault /home/klippervault/.local/share/klippervault \
    && chown -R klippervault:klippervault /app /home/klippervault/.config /home/klippervault/.local

USER klippervault

EXPOSE 10090

CMD ["python3", "klipper_vault_gui.py"]
