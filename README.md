# ThinQ Smart Home Control

[![CI](https://github.com/sanjc/thinq-smart-home-control/actions/workflows/ci.yml/badge.svg)](https://github.com/sanjc/thinq-smart-home-control/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Local Flask UI for controlling LG ThinQ ovens via the community ThinQ Connect API client (`thinqconnect`).

Built in collaboration with Codex (AI coding assistant).

## Setup

```bash
cd ~/thinq-oven-ui
cp .env.example .env

# Optional: edit .env to set your token, client ID, and country

source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5055`.

## Roadmap

See `ROADMAP.md`.

## Access token + client ID

This app does **not** store your ThinQ password. It only uses:

- `LG_THINQ_ACCESS_TOKEN`
- `LG_THINQ_CLIENT_ID`
- `LG_THINQ_COUNTRY`

ThinQ Connect expects a Personal Access Token (PAT) from the LG ThinQ Developer portal. Use the same LG account you use in the ThinQ app:

1. Visit the LG ThinQ Developer Site: https://smartsolution.developer.lge.com
2. Navigate to Cloud Developer → Docs → ThinQ Connect.
3. Generate a Personal Access Token.

Client ID should be a unique UUID (the setup screen generates one for you).

If you already use Home Assistant or another ThinQ integration, you can reuse the access token and client ID from that setup. Keep the token private and rotate it if it ever leaks.

## Notes

- ThinQ often enforces remote-start safety steps in the official app. Make sure the oven is safe to start before sending commands.
- The UI binds to `127.0.0.1` only. If you need remote access, use an SSH tunnel rather than exposing the port directly.
