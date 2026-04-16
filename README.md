# h2Radnor RNS Monitor

LSE Regulatory News Service announcement tracker for capital markets workflow.

## Quick Start (local)

```bash
pip install -r requirements.txt
python app.py
```

Opens at http://localhost:8000

## Deploy to Render

1. Push this repo to GitHub
2. Connect the repo in Render dashboard
3. Set environment variable `APP_PASSWORD` to your chosen password
4. Deploy

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_PASSWORD` | Optional | Set to enable password protection |
| `PORT` | Auto | Set by Render automatically |

## Project Structure

```
app.py              — Combined backend + API server
static/index.html   — React frontend dashboard
requirements.txt    — Python dependencies
render.yaml         — Render deployment config
```
