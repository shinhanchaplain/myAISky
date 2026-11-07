# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install: `pip install -r requirements.txt`
- Run dev server: `uvicorn app:app --reload --host 0.0.0.0 --port 8000`
- Dashboard: http://localhost:8000 — JSON: http://localhost:8000/api/weather
- No test, lint, or build setup exists yet.

## Architecture

Single-file FastAPI app (`app.py`) with a local SQLite log and no frontend build step.

- **Weather source:** `fetch_kma_current()` calls the 기상청 초단기실황 endpoint (`getUltraSrtNcst`) using `KMA_SERVICE_KEY` (Decoding key), `KMA_NX`/`KMA_NY` grid coords (default 55/127, Seoul). If the key is unset it returns fixed demo data with `source="demo"` instead of failing.
- **Sky status:** when PTY (강수형태) is 0, `_fetch_sky_label()` queries the 초단기예보 endpoint (`getUltraSrtFcst`) for the nearest SKY value (맑음/구름많음/흐림) and uses it as `status`. If that call fails it logs a warning and falls back to the PTY label ("강수 없음").
- **Base time:** the 실황 API publishes the current hour only after minute 40, so `_kma_base_datetime()` uses the previous hour before that. The 예보 call uses base time `HH30`, available after minute 45.
- **Persistence:** `init_db()` runs at import and creates `weather_logs` (`temperature`, `condition`, `timestamp UNIQUE`) in `weather_logs.db` (git-ignored). `/api/weather` calls `save_weather_log()` only for `source == "KMA"` observations with a temperature, so demo data is never stored; duplicate observation times are ignored (`INSERT OR IGNORE`). DB errors are logged, not raised. Nothing reads the table back yet.
- **Errors:** upstream failures raise `WeatherAPIError`, which `/api/weather` converts to HTTP 502.
- **Dashboard:** `DASHBOARD_HTML` is an inline string served at `/`. A hero card shows the current temperature, condition and an emoji icon (mapped from `status` via the `ICONS` table in JS). Its JS polls `/api/weather` every 10 minutes. The trend chart and "리포트 생성" report use only in-browser session samples, not the DB.
- **Static files:** `images/` is mounted at `/images`; the page uses `images/sky.jpg` as its full-page background.
- Comments and UI strings are in Korean; keep that convention when editing.
