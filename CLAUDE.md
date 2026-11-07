# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install: `pip install -r requirements.txt`
- Run dev server: `uvicorn app:app --reload --host 0.0.0.0 --port 8000`
- Dashboard: http://localhost:8000 — JSON: http://localhost:8000/api/weather
- No test, lint, or build setup exists yet.

## Architecture

Single-file FastAPI app (`app.py`) with no database or frontend build step.

- **Weather source:** `fetch_kma_current()` calls the 기상청 초단기실황 endpoint (`getUltraSrtNcst`) using `KMA_SERVICE_KEY` (Decoding key), `KMA_NX`/`KMA_NY` grid coords (default 60/127, Seoul). If the key is unset it returns fixed demo data with `source="demo"` instead of failing.
- **Base time:** the API publishes the current hour only after minute 40, so `_kma_base_datetime()` uses the previous hour before that.
- **Errors:** upstream failures raise `WeatherAPIError`, which `/api/weather` converts to HTTP 502.
- **Dashboard:** `DASHBOARD_HTML` is an inline string served at `/`. Its JS polls `/api/weather` every 10 minutes. The trend chart and "리포트 생성" report use only in-browser session samples, so nothing is persisted server-side.
- **Static files:** `images/` is mounted at `/images`; the page uses `images/sky.jpg` as its full-page background.
- Comments and UI strings are in Korean; keep that convention when editing.
