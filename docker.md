# Docker Deployment

KlipperVault supports Docker deployment in remote-only `off_printer` mode.

Included files:

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

## 1) Build and start

From repository root:

```bash
docker compose up -d --build
```

Web UI will be available on:

```text
http://<host-ip>:10090
```

## 2) Persistent data

Compose defines two named volumes:

- `klippervault_config` -> `/data/config`
- `klippervault_db` -> `/data/db`

The container maps these to runtime environment variables:

- `KLIPPERVAULT_CONFIG_DIR=/data/config`
- `KLIPPERVAULT_DB_PATH=/data/db/klipper_macros.db`

This keeps settings, profiles, and macro history across container restarts/rebuilds.

## 3) Logs and lifecycle

```bash
docker compose logs -f klippervault
docker compose restart klippervault
docker compose down
```

## 4) Networking requirements

The container must be able to reach:

- Printer SSH/SFTP endpoint (typically port 22)
- Moonraker HTTP endpoint (for printer state/actions)

If your printer is on another VLAN/subnet, allow outbound access from the Docker host/container network.

## 5) Upgrade flow

```bash
git pull
docker compose up -d --build
```

## 6) First-run checklist

1. Open KlipperVault UI.
2. Open `Printers` -> `Manage printer connections`.
3. Add and save a printer profile.
4. Test printer connection.
5. Run `Scan macros`.
