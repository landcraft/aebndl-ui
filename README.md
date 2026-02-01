# aebndl-ui

A high-performance, containerized Web UI for the [aebn-vod-downloader](https://github.com/hyper440/aebn-vod-downloader).

## Features
- **Smart Source Tracking**: Automatically checks upstream for updates and rebuilds.
- **Resilient**: Falls back to local backups if upstream is unreachable.
- **Web Dashboard**: Clean interface to queue downloads.
- **Containerized**: Runs anywhere with Docker.

## Usage

### With Docker (Recommended)

Create a `compose.yaml` file:

```yaml
services:
  aebndl-ui:
    image: ghcr.io/landcraft/aebndl-ui:latest
    container_name: aebndl-ui
    ports:
      - "21345:21345"
    volumes:
      - ./downloads:/downloads
    environment:
      DOWNLOAD_DIR: '/downloads'
    restart: unless-stopped
```

Run it:
```bash
docker compose up -d
```
Access the dashboard at `http://localhost:21345`.

### Manual
1. Install dependencies:
   ```bash
   pip install -r src/requirements.txt
   ```
2. Run the server:
   ```bash
   python3 src/main.py
   ```
   (Note: You might need to run `uvicorn src.main:app` directly if main.py doesn't start uvicorn programmatically, or use the `CMD` from Dockerfile instructions).

## Configuration
- `DOWNLOAD_DIR`: Path to save downloads (default: `./downloads`).

The project includes an `update_source.sh` script that runs automatically in the Docker build process/CI to fetch the latest upstream downloader code.

## Attribution
This project uses the core downloading logic from the **aebn-vod-downloader** project.
- **Original Source**: [https://github.com/hyper440/aebn-vod-downloader](https://github.com/hyper440/aebn-vod-downloader)
- **Credits**: `estellaarrieta`, `hyper440`

See [NOTICE](NOTICE) and [ATTRIBUTION.md](ATTRIBUTION.md) for more details.
