"""백그라운드 스케줄러 검증. 외부 네트워크와 실제 DB 파일을 사용하지 않는다.

실행: python tests/test_scheduler.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "TEST-SECRET-KEY-12345"
_TMP = tempfile.mkdtemp(prefix="myaisky_sched_test_")
_TMP_DB = os.path.join(_TMP, "test_weather_logs.db")

# import 시점의 init_db()가 실제 weather_logs.db를 건드리지 않도록 임시 경로로 우회
_real_connect = sqlite3.connect
with mock.patch("sqlite3.connect", lambda *a, **k: _real_connect(_TMP_DB)):
    import app as appmod

from fastapi.testclient import TestClient  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Error for url: https://x/?serviceKey={SECRET}")
            err.response = self
            raise err

    def json(self):
        return self._payload


def ncst_payload():
    items = [{"category": c, "obsrValue": v} for c, v in
             [("T1H", "20.1"), ("REH", "50"), ("WSD", "1.5"), ("RN1", "0"), ("PTY", "1")]]
    return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": items}}}}


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        appmod.DB_PATH = Path(_TMP_DB)
        appmod.init_db()
        conn = _real_connect(_TMP_DB)
        conn.execute("DELETE FROM weather_logs")
        conn.commit()
        conn.close()
        appmod._last_kma_obs = None
        self.env = mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": SECRET})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        appmod.stop_scheduler()

    def rows(self):
        conn = _real_connect(_TMP_DB)
        try:
            return conn.execute("SELECT COUNT(*) FROM weather_logs").fetchone()[0]
        finally:
            conn.close()

    def test_1_job_success_saves_and_caches(self):
        with mock.patch.object(appmod.requests, "get", return_value=FakeResp(payload=ncst_payload())):
            appmod.scheduled_weather_job()
        self.assertIsNotNone(appmod._last_kma_obs)
        self.assertEqual(appmod._last_kma_obs.source, "KMA")
        self.assertEqual(self.rows(), 1)

    def test_2_job_403_is_swallowed_and_cache_kept(self):
        with mock.patch.object(appmod.requests, "get", return_value=FakeResp(payload=ncst_payload())):
            appmod.scheduled_weather_job()
        before = appmod._last_kma_obs
        with mock.patch.object(appmod.requests, "get", return_value=FakeResp(status=403)):
            with self.assertLogs("myAISky", level="ERROR") as logs:
                appmod.scheduled_weather_job()  # 예외가 새어 나오면 안 됨
        self.assertIs(appmod._last_kma_obs, before)
        self.assertEqual(self.rows(), 1)
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_3_job_unexpected_error_is_swallowed(self):
        with mock.patch.object(appmod, "fetch_kma_current", side_effect=RuntimeError("boom")):
            appmod.scheduled_weather_job()
        self.assertIsNone(appmod._last_kma_obs)

    def test_4_job_demo_not_saved_or_cached(self):
        with mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": ""}):
            appmod.scheduled_weather_job()
        self.assertIsNone(appmod._last_kma_obs)
        self.assertEqual(self.rows(), 0)

    def test_5_lifespan_starts_and_stops_scheduler(self):
        with mock.patch.object(appmod, "scheduled_weather_job"):
            with TestClient(appmod.app):
                sched = appmod._scheduler
                self.assertIsNotNone(sched)
                self.assertTrue(sched.running)
                job = sched.get_job("weather_collect")
                self.assertEqual(job.max_instances, 1)
                self.assertTrue(job.coalesce)
                # 중복 기동 방지: 다시 호출해도 같은 인스턴스
                self.assertIs(appmod.start_scheduler(), sched)
        self.assertIsNone(appmod._scheduler)
        self.assertFalse(sched.running)


if __name__ == "__main__":
    unittest.main(verbosity=2)