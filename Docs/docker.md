# Docker Deployment

KlipperVault supports Docker deployment for standard remote profiles and developer-mode virtual local-only profiles.

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

## 2) Install from Docker Hub

Published image:

```text
shadowrom2020/klippervault:latest
```

Pull and run directly:

```bash
docker pull shadowrom2020/klippervault:latest

docker run -d \
	--name klippervault \
	--restart unless-stopped \
	-p 10090:10090 \
	-e KLIPPERVAULT_SERVER_MODE=1 \
	-v klippervault_config:/home/klippervault/.config/klippervault \
	-v klippervault_db:/home/klippervault/.local/share/klippervault \
	shadowrom2020/klippervault:latest
```

Or use Compose with Docker Hub image (no local build):

```yaml
services:
	klippervault:
		image: shadowrom2020/klippervault:latest
		container_name: klippervault
		restart: unless-stopped
		environment:
			KLIPPERVAULT_SERVER_MODE: "1"
		ports:
			- "10090:10090"
		volumes:
			- klippervault_config:/home/klippervault/.config/klippervault
			- klippervault_db:/home/klippervault/.local/share/klippervault

volumes:
	klippervault_config:
	klippervault_db:
```

Then start it:

```bash
docker compose up -d
```

## 3) Persistent data

Compose defines two named volumes:

- `klippervault_config` -> `/home/klippervault/.config/klippervault`
- `klippervault_db` -> `/home/klippervault/.local/share/klippervault`

This keeps settings, profiles, and macro history across container restarts/rebuilds.

## 4) Logs and lifecycle

```bash
docker compose logs -f klippervault
docker compose restart klippervault
docker compose down
```

## 5) Networking requirements

The container must be able to reach:

- Printer SSH/SFTP endpoint (typically port 22)
- Moonraker HTTP endpoint (for printer state/actions)

If your printer is on another VLAN/subnet, allow outbound access from the Docker host/container network.

## 6) Upgrade flow

```bash
git pull
docker compose up -d --build
```

## 7) First-run checklist

1. Open KlipperVault UI.
2. Open `Printers` -> `Manage printer connections`.
3. Add and save a printer profile.
4. Test printer connection.
5. Run `Scan macros`.
