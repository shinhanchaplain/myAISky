"""KMA 상류 실패 시 캐시 폴백 검증. 외부 네트워크와 실제 DB 파일을 사용하지 않는다.

실행: python tests/test_weather_fallback.py
"""

import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "TEST-SECRET-KEY-12345"
_TMP = tempfile.mkdtemp(prefix="myaisky_test_")
_TMP_DB = os.path.join(_TMP, "test_weather_logs.db")

# import 시점의 init_db()가 실제 weather_logs.db를 건드리지 않도록 임시 경로로 우회
_real_connect = sqlite3.connect
with mock.patch("sqlite3.connect", lambda *a, **k: _real_connect(_TMP_DB)):
    import app as appmod

from fastapi.testclient import TestClient  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Error for url: https://x/?serviceKey={SECRET}")
            err.response = self
            raise err

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def ncst_payload():
    items = [{"category": c, "obsrValue": v} for c, v in
             [("T1H", "20.1"), ("REH", "50"), ("WSD", "1.5"), ("RN1", "0"), ("PTY", "1")]]
    return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": items}}}}


class FallbackTest(unittest.TestCase):
    def setUp(self):
        appmod.DB_PATH = Path(_TMP_DB)
        appmod.init_db()
        conn = _real_connect(_TMP_DB)  # Windows에서 파일 삭제가 막히므로 행만 비운다
        conn.execute("DELETE FROM weather_logs")
        conn.commit()
        conn.close()
        appmod._last_kma_obs = None
        self.client = TestClient(appmod.app)
        self.env = mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": SECRET})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def rows(self):
        conn = _real_connect(_TMP_DB)
        try:
            return conn.execute("SELECT COUNT(*) FROM weather_logs").fetchone()[0]
        finally:
            conn.close()

    def get(self, resp):
        with mock.patch.object(appmod.requests, "get", return_value=resp):
            return self.client.get("/api/weather")

    def test_1_success_fills_cache(self):
        r = self.get(FakeResp(payload=ncst_payload()))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "KMA")
        self.assertIsNotNone(appmod._last_kma_obs)
        self.assertEqual(self.rows(), 1)

    def test_2_403_falls_back_to_cache_without_db_write(self):
        self.get(FakeResp(payload=ncst_payload()))
        self.assertEqual(self.rows(), 1)
        with self.assertLogs("myAISky", level=logging.INFO) as logs:
            r = self.get(FakeResp(status=403))
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["source"], "cache")
        self.assertIsNotNone(body["cache_age_minutes"])
        self.assertIn("캐시", body["note"])
        self.assertEqual(self.rows(), 1)  # 폴백은 DB에 저장되지 않음
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, r.text)
        self.assertIn("HTTP 403", "\n".join(logs.output))

    def test_3_timeout_and_non_json_fall_back(self):
        self.get(FakeResp(payload=ncst_payload()))
        with mock.patch.object(appmod.requests, "get", side_effect=requests.Timeout(SECRET)):
            self.assertEqual(self.client.get("/api/weather").json()["source"], "cache")
        self.assertEqual(self.get(FakeResp(json_error=True)).json()["source"], "cache")

    def test_4_no_cache_returns_502(self):
        r = self.get(FakeResp(status=403))
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(SECRET, r.text)
        self.assertEqual(self.rows(), 0)

    def test_5_expired_cache_returns_502(self):
        self.get(FakeResp(payload=ncst_payload()))
        old = (datetime.now(appmod.KST) - timedelta(hours=3, minutes=5)).strftime("%Y-%m-%d %H:%M")
        appmod._last_kma_obs = appmod._last_kma_obs.model_copy(update={"observed_at": old})
        r = self.get(FakeResp(status=403))
        self.assertEqual(r.status_code, 502)

    def test_6_demo_not_cached(self):
        with mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": ""}):
            r = self.client.get("/api/weather")
        self.assertEqual(r.json()["source"], "demo")
        self.assertIsNone(appmod._last_kma_obs)
        self.assertEqual(self.rows(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)