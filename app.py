"""myAISky - AI Weather & Agent Monitoring Dashboard (1교시 백엔드).

- GET /            : 대시보드 HTML (배경: images/sky.jpg)
- GET /api/weather : 기상청 초단기실황 기반 현재 날씨 JSON
- 정적 파일        : /images/* -> ./images/*

환경 변수
- KMA_SERVICE_KEY : 공공데이터포털 기상청 API 인증키 (Decoding 키 권장).
                    미설정 시 데모 데이터를 반환합니다.
- KMA_NX, KMA_NY  : 기상청 격자 좌표 (기본 55, 127 = 서울)
- 실측 데이터는 weather_logs.db 의 weather_logs 테이블에 자동 저장됩니다.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("myAISky")

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "images"
KST = timezone(timedelta(hours=9))

KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_URL = f"{KMA_BASE_URL}/getUltraSrtNcst"      # 초단기실황
KMA_FCST_URL = f"{KMA_BASE_URL}/getUltraSrtFcst"  # 초단기예보 (하늘상태 SKY 용)
KMA_NX = int(os.getenv("KMA_NX", "55"))
KMA_NY = int(os.getenv("KMA_NY", "127"))

DB_PATH = BASE_DIR / "weather_logs.db"

# 초단기예보 하늘상태(SKY) 코드
SKY_LABELS = {1: "맑음", 3: "구름많음", 4: "흐림"}

# 초단기실황 강수형태(PTY) 코드
PTY_LABELS = {
    0: "강수 없음",
    1: "비",
    2: "비/눈",
    3: "눈",
    5: "빗방울",
    6: "빗방울/눈날림",
    7: "눈날림",
}


class WeatherAPIError(RuntimeError):
    """기상청 API 호출 또는 응답 해석 실패."""


class WeatherObservation(BaseModel):
    source: str                      # "KMA", "demo" 또는 "cache"
    observed_at: str
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_ms: float | None
    precipitation_mm: float | None
    precipitation_type: int | None
    status: str
    note: str | None = None
    cache_age_minutes: int | None = None  # source="cache"일 때 관측 후 경과 시간(분)


# 마지막으로 성공한 실제 KMA 관측 (메모리 캐시). 데모/캐시 데이터는 넣지 않는다.
_last_kma_obs: WeatherObservation | None = None
CACHE_MAX_AGE = timedelta(hours=3)


def _cache_fallback(now: datetime | None = None) -> WeatherObservation | None:
    """상류 실패 시 사용할 캐시 관측을 반환. 없거나 3시간 초과면 None."""
    obs = _last_kma_obs
    if obs is None:
        return None
    now = now or datetime.now(KST)
    try:
        observed = datetime.strptime(obs.observed_at, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return None
    age = now - observed
    if age > CACHE_MAX_AGE:
        return None
    minutes = max(int(age.total_seconds() // 60), 0)
    return obs.model_copy(update={
        "source": "cache",
        "cache_age_minutes": minutes,
        "note": f"기상청 호출 실패로 캐시 사용 (관측 {obs.observed_at}, {minutes}분 경과)",
    })


def init_db() -> None:
    """weather_logs 테이블 생성. 같은 관측 시각(timestamp)은 한 번만 저장한다."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                temperature REAL,
                condition TEXT NOT NULL,
                timestamp TEXT NOT NULL UNIQUE
            )
            """
        )


def save_weather_log(obs: WeatherObservation) -> bool:
    """실제 기상청 관측값을 DB에 INSERT. 저장되면 True, 중복이면 False."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO weather_logs (temperature, condition, timestamp) VALUES (?, ?, ?)",
            (obs.temperature_c, obs.status, obs.observed_at),
        )
        return cur.rowcount > 0


def _to_float(value: object) -> float | None:
    """기상청 응답의 '-', '강수없음' 등 비수치 값을 None으로 변환."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _kma_base_datetime(now: datetime) -> tuple[str, str]:
    """초단기실황은 매시 40분 이후에 해당 시각 자료가 제공되므로 그 전에는 직전 정시를 사용."""
    base = now - timedelta(hours=1) if now.minute < 40 else now
    return base.strftime("%Y%m%d"), base.strftime("%H00")


def _fetch_sky_label(service_key: str, now: datetime) -> str | None:
    """초단기예보에서 가장 가까운 시각의 하늘상태(맑음/구름많음/흐림)를 조회. 실패 시 None."""
    # 초단기예보는 매시 30분 발표, 45분 이후 제공
    base = now - timedelta(hours=1) if now.minute < 45 else now
    params = {
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": 60,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base.strftime("%H30"),
        "nx": KMA_NX,
        "ny": KMA_NY,
    }
    try:
        resp = requests.get(KMA_FCST_URL, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json()["response"]["body"]["items"]["item"]
        sky = [i for i in items if i["category"] == "SKY"]
        nearest = min(sky, key=lambda i: i["fcstTime"])
        return SKY_LABELS.get(int(nearest["fcstValue"]))
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        # 예외 문자열에 serviceKey 포함 URL이 들어갈 수 있어 예외 종류만 기록
        logger.warning("하늘상태 조회 실패(강수 상태만 사용): %s", type(exc).__name__)
        return None


def fetch_kma_current(now: datetime | None = None) -> WeatherObservation:
    """기상청 초단기실황 API로 현재 기온/습도/풍속/강수 정보를 가져온다."""
    now = now or datetime.now(KST)
    service_key = os.getenv("KMA_SERVICE_KEY")
    if not service_key:
        logger.warning("KMA_SERVICE_KEY 미설정 - 데모 데이터를 반환합니다.")
        return WeatherObservation(
            source="demo",
            observed_at=now.strftime("%Y-%m-%d %H:%M"),
            temperature_c=18.5,
            humidity_pct=56.0,
            wind_speed_ms=2.3,
            precipitation_mm=0.0,
            precipitation_type=0,
            status=PTY_LABELS[0],
            note="KMA_SERVICE_KEY 미설정: 데모 데이터",
        )

    base_date, base_time = _kma_base_datetime(now)
    params = {
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": 100,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": KMA_NX,
        "ny": KMA_NY,
    }

    try:
        resp = requests.get(KMA_URL, params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except requests.HTTPError as exc:
        # 예외 문자열에는 serviceKey가 담긴 URL이 들어 있으므로 상태 코드만 사용
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            logger.error("기상청 API 인증 거부(HTTP %s): 인증키 미등록/만료/활용신청 승인 여부를 확인하세요.", status)
            raise WeatherAPIError(f"기상청 API 인증 거부(HTTP {status})") from None
        logger.error("기상청 API HTTP 오류: %s", status)
        raise WeatherAPIError(f"기상청 API HTTP 오류({status})") from None
    except requests.Timeout:
        logger.error("기상청 API 타임아웃")
        raise WeatherAPIError("기상청 API 타임아웃") from None
    except requests.RequestException as exc:
        logger.error("기상청 API 연결 실패: %s", type(exc).__name__)
        raise WeatherAPIError(f"기상청 API 연결 실패({type(exc).__name__})") from None
    except ValueError:
        # 200 이지만 JSON이 아닌 경우 (XML 에러 응답 등)
        logger.error("기상청 API 응답이 JSON이 아님 (XML 에러 응답 가능: 인증키 오류 등)")
        raise WeatherAPIError("기상청 API 응답 해석 실패(JSON 아님)") from None

    response = payload.get("response", {})
    header = response.get("header", {})
    if header.get("resultCode") != "00":
        raise WeatherAPIError(f"기상청 API 오류: {header.get('resultMsg', 'unknown')}")

    try:
        items = response.get("body", {}).get("items", {}).get("item", [])
        values = {item["category"]: item["obsrValue"] for item in items}
    except (AttributeError, KeyError, TypeError) as exc:
        raise WeatherAPIError(f"기상청 API 응답 구조 오류({type(exc).__name__})") from None

    pty = int(_to_float(values.get("PTY")) or 0)
    # 강수가 있으면 강수 형태, 없으면 하늘상태(맑음/구름많음/흐림)를 날씨 상태로 사용
    status = PTY_LABELS.get(pty, "알 수 없음")
    if pty == 0:
        status = _fetch_sky_label(service_key, now) or status
    return WeatherObservation(
        source="KMA",
        observed_at=f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]} {base_time[:2]}:{base_time[2:]}",
        temperature_c=_to_float(values.get("T1H")),
        humidity_pct=_to_float(values.get("REH")),
        wind_speed_ms=_to_float(values.get("WSD")),
        precipitation_mm=_to_float(values.get("RN1")),
        precipitation_type=pty,
        status=status,
        note="기상청 초단기실황",
    )


# ---------------------------------------------------------------------------
# Dashboard HTML (배경: /images/sky.jpg)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>myAISky</title>
<style>
  :root {
    --cream: #F8F5EE;
    --ink: #2F2A24;
    --muted: #7A7267;
    --accent: #C98B4B;
    --charcoal: #24272E;
    --line: #E6DED0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: "Pretendard", "Malgun Gothic", "Segoe UI", sans-serif;
    color: var(--ink);
    background-image: url('/images/sky.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    background-repeat: no-repeat;
  }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 32px 20px; }
  .container {
    background: rgba(248, 245, 238, 0.94);
    border-radius: 24px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.25);
    padding: 28px;
  }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
  h1 { margin: 0; font-size: 32px; letter-spacing: -0.5px; }
  .subtitle { margin: 4px 0 0; color: var(--muted); font-size: 15px; }
  .badge {
    background: #E7F5EA; color: #21673A; border: 1px solid #BFE3CB;
    padding: 8px 14px; border-radius: 999px; font-size: 14px; font-weight: 600; white-space: nowrap;
  }
  .hero {
    display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap;
    background: linear-gradient(135deg, #4A8FD6 0%, #7DB8EA 55%, #BFDDF5 100%);
    color: #fff; border-radius: 20px; padding: 24px 32px; margin-bottom: 20px;
    box-shadow: 0 10px 30px rgba(74, 143, 214, 0.35);
  }
  .hero .icon { font-size: 84px; line-height: 1; filter: drop-shadow(0 4px 8px rgba(0,0,0,0.2)); }
  .hero .temp { font-size: 76px; font-weight: 800; letter-spacing: -2px; line-height: 1; }
  .hero .cond { font-size: 22px; font-weight: 600; margin-top: 6px; }
  .hero .where { font-size: 14px; opacity: 0.9; text-align: right; line-height: 1.7; }
  .hero .left { display: flex; align-items: center; gap: 22px; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
  .stat { background: #fff; border-radius: 16px; padding: 16px 18px; border: 1px solid var(--line); }
  .stat .label { font-size: 13px; color: var(--muted); }
  .stat .value { font-size: 24px; font-weight: 700; margin-top: 6px; }
  .main { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; }
  .card { background: #fff; border-radius: 18px; padding: 20px; border: 1px solid var(--line); }
  .card h2 { margin: 0 0 6px; font-size: 18px; }
  .trend {
    display: flex; align-items: flex-end; gap: 8px; height: 220px; padding-top: 10px;
    border-bottom: 1px solid var(--line);
  }
  .trend .empty { color: var(--muted); font-size: 14px; align-self: center; width: 100%; text-align: center; }
  .bar-wrap { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; height: 100%; }
  .bar { width: 100%; max-width: 40px; background: linear-gradient(180deg, #E9B872, var(--accent)); border-radius: 8px 8px 0 0; }
  .bar-val { font-size: 11px; color: var(--ink); margin-bottom: 4px; }
  .bar-label { font-size: 11px; color: var(--muted); margin-top: 6px; }
  .agent {
    background: var(--charcoal); color: #D6DCE5; border-radius: 18px; padding: 20px;
    font-family: Consolas, "Courier New", monospace; font-size: 13px; display: flex; flex-direction: column;
  }
  .agent h2 { color: #fff; font-family: "Malgun Gothic", sans-serif; font-size: 18px; margin: 0 0 12px; }
  .log { overflow-y: auto; max-height: 320px; line-height: 1.7; }
  .log .ts { color: #7FA7D9; }
  .log .ok { color: #8FD19E; }
  .log .warn { color: #E8B66B; }
  .log .err { color: #E88A8A; }
  .control {
    margin-top: 20px; background: #fff; border-radius: 18px; padding: 20px; border: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }
  .control h2 { margin: 0; font-size: 18px; }
  .control p { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
  .btns { display: flex; gap: 10px; }
  button { border: none; border-radius: 12px; padding: 12px 18px; font-size: 14px; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: wait; }
  .btn-primary { background: var(--charcoal); color: #fff; }
  .btn-ghost { background: #EFE8DA; color: var(--ink); }
  @media (max-width: 860px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .main { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="container">
    <header>
      <div>
        <h1>myAISky</h1>
        <p class="subtitle">AI Weather &amp; Agent Monitoring Dashboard</p>
      </div>
      <span class="badge">🟢 24/7 Sandbox Active</span>
    </header>

    <section class="hero">
      <div class="left">
        <div class="icon" id="h-icon">⛅</div>
        <div>
          <div class="temp" id="h-temp">--</div>
          <div class="cond" id="h-cond">불러오는 중...</div>
        </div>
      </div>
      <div class="where" id="h-where">서울 · 기상청 실시간</div>
    </section>

    <section class="stats">
      <div class="stat"><div class="label">현재 기온</div><div class="value" id="s-temp">--</div></div>
      <div class="stat"><div class="label">습도</div><div class="value" id="s-hum">--</div></div>
      <div class="stat"><div class="label">풍속</div><div class="value" id="s-wind">--</div></div>
      <div class="stat"><div class="label">강수 상태</div><div class="value" id="s-status">--</div></div>
    </section>

    <section class="main">
      <div class="card">
        <h2>실시간 기상 데이터 분석 · 온도 트렌드</h2>
        <p class="subtitle" id="meta" style="margin: 0 0 12px;">데이터를 불러오는 중...</p>
        <div class="trend" id="trend"></div>
      </div>
      <aside class="agent">
        <h2>AI 에이전트 실시간 로그</h2>
        <div class="log" id="log"></div>
      </aside>
    </section>

    <section class="control">
      <div>
        <h2>Manual Control Panel</h2>
        <p>기상 데이터 동기화 및 세션 리포트를 수동으로 실행합니다.</p>
      </div>
      <div class="btns">
        <button class="btn-ghost" id="btn-sync">데이터 동기화</button>
        <button class="btn-primary" id="btn-report">리포트 생성</button>
      </div>
    </section>
  </div>
</div>

<script>
const samples = [];          // {label, temp} 최근 관측값 (트렌드용)
const MAX_POINTS = 12;
const AUTO_SYNC_MS = 10 * 60 * 1000;
const $ = (id) => document.getElementById(id);
const fmt = (v, unit) => (v === null || v === undefined) ? "--" : `${v}${unit}`;
const timeLabel = () => new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });

function log(msg, cls = "") {
  const line = document.createElement("div");
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = `[${new Date().toLocaleTimeString("ko-KR", { hour12: false })}] `;
  const body = document.createElement("span");
  body.className = cls;
  body.textContent = msg;
  line.append(ts, body);
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

function drawTrend() {
  const box = $("trend");
  box.innerHTML = "";
  if (!samples.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "수집된 데이터가 없습니다.";
    box.appendChild(empty);
    return;
  }
  const temps = samples.map((s) => s.temp);
  const min = Math.min(...temps);
  const span = Math.max(Math.max(...temps) - min, 1);
  samples.forEach((s) => {
    const wrap = document.createElement("div");
    wrap.className = "bar-wrap";
    const val = document.createElement("div");
    val.className = "bar-val";
    val.textContent = `${s.temp}°`;
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.height = `${40 + ((s.temp - min) / span) * 150}px`;
    const label = document.createElement("div");
    label.className = "bar-label";
    label.textContent = s.label;
    wrap.append(val, bar, label);
    box.appendChild(wrap);
  });
}

const ICONS = { "맑음": "☀️", "구름많음": "⛅", "흐림": "☁️", "비": "🌧️", "빗방울": "🌦️",
                "비/눈": "🌨️", "빗방울/눈날림": "🌨️", "눈": "❄️", "눈날림": "❄️", "강수 없음": "🌤️" };

function render(d) {
  $("h-temp").textContent = d.temperature_c === null ? "--" : `${d.temperature_c}°`;
  $("h-cond").textContent = d.status;
  $("h-icon").textContent = ICONS[d.status] || "🌡️";
  $("h-where").innerHTML = "";
  const srcLabel = d.source === "demo" ? "데모 데이터" : d.source === "cache" ? `캐시 (${d.cache_age_minutes}분 전 관측)` : "기상청 실시간";
  ["서울 · " + srcLabel, "관측 " + d.observed_at]
    .forEach((t) => { const r = document.createElement("div"); r.textContent = t; $("h-where").appendChild(r); });
  $("s-temp").textContent = fmt(d.temperature_c, "°C");
  $("s-hum").textContent = fmt(d.humidity_pct, "%");
  $("s-wind").textContent = fmt(d.wind_speed_ms, " m/s");
  $("s-status").textContent = d.status;
  $("meta").textContent = `${d.source === "demo" ? "[DEMO] " : d.source === "cache" ? "[캐시] " : ""}관측 ${d.observed_at} · ${d.note || ""}`;
  if (d.temperature_c !== null) {
    samples.push({ label: timeLabel(), temp: d.temperature_c });
    if (samples.length > MAX_POINTS) samples.shift();
  }
  drawTrend();
}

async function syncWeather() {
  $("btn-sync").disabled = true;
  log("기상 데이터 동기화 요청");
  try {
    const res = await fetch("/api/weather");
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    render(body);
    log(`동기화 완료: ${fmt(body.temperature_c, "°C")}, ${body.status}`, "ok");
  } catch (e) {
    log(`동기화 실패: ${e.message}`, "err");
  } finally {
    $("btn-sync").disabled = false;
  }
}

function generateReport() {
  if (!samples.length) {
    log("리포트 생성 실패: 수집된 데이터가 없습니다.", "warn");
    return;
  }
  const temps = samples.map((s) => s.temp);
  const avg = (temps.reduce((a, b) => a + b, 0) / temps.length).toFixed(1);
  log(`리포트 생성 완료 · 샘플 ${temps.length}건 · 평균 ${avg}°C · 최저 ${Math.min(...temps)}°C · 최고 ${Math.max(...temps)}°C`, "ok");
}

$("btn-sync").addEventListener("click", syncWeather);
$("btn-report").addEventListener("click", generateReport);
log("myAISky 에이전트 대기 중", "ok");
log("24/7 샌드박스 모니터링 시작");
syncWeather();
setInterval(syncWeather, AUTO_SYNC_MS);
</script>
</body>
</html>
"""

def fetch_and_record() -> WeatherObservation:
    """기상청 조회 후 실제 KMA 관측이면 캐시 갱신 및 DB 적재. 상류 실패 시 WeatherAPIError를 그대로 전파."""
    global _last_kma_obs
    obs = fetch_kma_current()
    # 데모 데이터는 캐시/DB를 오염시키지 않도록 실제 기상청 데이터만 반영
    if obs.source == "KMA" and obs.temperature_c is not None:
        _last_kma_obs = obs
        try:
            if save_weather_log(obs):
                logger.info("weather_logs 저장: %s %s°C %s", obs.observed_at, obs.temperature_c, obs.status)
        except sqlite3.Error as exc:
            logger.error("DB 저장 실패: %s", exc)
    return obs


SCHEDULER_INTERVAL_MIN = 10
_scheduler: BackgroundScheduler | None = None


def scheduled_weather_job() -> None:
    """10분 주기 백그라운드 잡. 예외는 로그만 남기고 스케줄러가 죽지 않게 한다."""
    try:
        obs = fetch_and_record()
        logger.info("스케줄러 수집 완료: source=%s", obs.source)
    except WeatherAPIError as exc:
        logger.error("스케줄러 수집 실패 (캐시 유지): %s", exc)
    except Exception:  # noqa: BLE001 - 어떤 오류든 스케줄러를 멈추지 않는다
        logger.exception("스케줄러 잡 예기치 않은 오류")


def start_scheduler() -> BackgroundScheduler:
    """스케줄러 시작 (이미 실행 중이면 재기동하지 않음). 시작 직후 1회 즉시 실행해 캐시를 시드한다."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    sched = BackgroundScheduler(timezone=KST)
    sched.add_job(
        scheduled_weather_job,
        "interval",
        minutes=SCHEDULER_INTERVAL_MIN,
        id="weather_collect",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=datetime.now(KST),  # 시작 즉시 1회 실행 (스케줄러 스레드에서 수행)
    )
    sched.start()
    _scheduler = sched
    logger.info("백그라운드 스케줄러 시작 (%s분 주기)", SCHEDULER_INTERVAL_MIN)
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("백그라운드 스케줄러 종료")
    _scheduler = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


init_db()
app = FastAPI(title="myAISky", version="0.1.0", lifespan=lifespan)
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/api/weather", response_model=WeatherObservation)
def get_weather() -> WeatherObservation:
    try:
        obs = fetch_and_record()
    except WeatherAPIError as exc:
        logger.error("%s", exc)
        cached = _cache_fallback()
        if cached is not None:
            logger.warning("캐시 폴백 반환 (%s분 경과), DB에는 저장하지 않음", cached.cache_age_minutes)
            return cached  # 캐시는 DB에 재적재하지 않는다
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return obs


if __name__ == "__main__":
    import uvicorn

    print(
        "\n== myAISky 실행 방법 ==\n"
        "1) 의존성 설치 : pip install -r requirements.txt\n"
        "2) (선택) 키 설정 : set KMA_SERVICE_KEY=발급받은_디코딩_키   (PowerShell: $env:KMA_SERVICE_KEY=\"...\")\n"
        "3) 서버 실행   : uvicorn app:app --reload --host 0.0.0.0 --port 8000\n"
        "4) 접속        : http://localhost:8000  ,  API: http://localhost:8000/api/weather\n"
    )
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
