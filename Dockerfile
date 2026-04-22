FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    KLIPPERVAULT_CONTAINER=1

WORKDIR /app

RUN useradd --create-home --home-dir /home/klippervault --shell /usr/sbin/nologin klippervault

COPY requirements.txt ./
RUN python3 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /home/klippervault/.config/klippervault /home/klippervault/.local/share/klippervault \
    && chown -R klippervault:klippervault /app /home/klippervault/.config /home/klippervault/.local "$VIRTUAL_ENV"

USER klippervault

EXPOSE 10090

CMD ["python3", "klipper_vault_gui.py"]
