import os
import unittest
import asyncio
from unittest.mock import MagicMock, patch
from collections import deque

# Set test environment
os.environ["DOWNLOAD_DIR"] = "./test_downloads"

from src.main import DownloadManager, app, verify_auth, TEMP_BASE
from fastapi import HTTPException


class TestDownloadManager(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager(max_concurrent=1)

    def tearDown(self):
        self.manager.shutdown()

    def test_add_job_initialization(self):
        job_id = self.manager.add_job(
            url="https://aebn.net/test-video",
            threads=5,
            resolution="1080",
            scene="2",
            output_dir="./test_downloads",
            names=True,
            covers=True,
            split_scenes=False
        )
        self.assertRegex(job_id, r"^[a-f0-9]{8}$")
        self.assertIn(job_id, self.manager.active_jobs)
        job = self.manager.active_jobs[job_id]
        self.assertEqual(job["url"], "https://aebn.net/test-video")
        self.assertEqual(job["threads"], 5)
        self.assertEqual(job["resolution"], "1080")
        self.assertEqual(job["scene"], "2")
        self.assertTrue(job["names"])
        self.assertTrue(job["covers"])
        self.assertFalse(job["split_scenes"])
        self.assertEqual(job["status"], "queued")
        self.assertIsInstance(job["logs"], deque)

    def test_path_traversal_rejection_in_delete(self):
        # Malicious job IDs with path traversal attempts
        malicious_ids = [
            "../../../etc/passwd",
            "..",
            "../temp",
            "12345/../../",
            "non-hex-id-too-long-12345678",
            "../../",
        ]
        for bad_id in malicious_ids:
            with self.subTest(bad_id=bad_id):
                result = self.manager.delete_job(bad_id)
                self.assertFalse(result, f"delete_job should have rejected bad_id: {bad_id}")

    def test_get_job_logs_sanitization(self):
        # Non-matching job ID
        self.assertIsNone(self.manager.get_job_logs("../../invalid"))
        
        # Add valid job and append logs
        job_id = self.manager.add_job(
            url="https://aebn.net/movie",
            threads=5,
            resolution="720",
            scene="1",
            output_dir="./test_downloads"
        )
        self.manager.active_jobs[job_id]["logs"].append("Line 1")
        self.manager.active_jobs[job_id]["logs"].append("Line 2")
        
        logs = self.manager.get_job_logs(job_id)
        self.assertEqual(logs, ["Line 1", "Line 2"])

    def test_clear_completed_jobs(self):
        # Create completed jobs in history
        job1 = {"id": "11111111", "status": "completed", "completed_at": 100, "url": "https://aebn.net/1"}
        job2 = {"id": "22222222", "status": "failed", "url": "https://aebn.net/2"}
        job3 = {"id": "33333333", "status": "completed", "completed_at": 200, "url": "https://aebn.net/3"}

        self.manager.history["11111111"] = job1
        self.manager.history["22222222"] = job2
        self.manager.history["33333333"] = job3

        self.manager.clear_completed_jobs()

        self.assertNotIn("11111111", self.manager.history)
        self.assertIn("22222222", self.manager.history)  # Failed job stays
        self.assertNotIn("33333333", self.manager.history)

    def test_status_sanitization_excludes_heavy_logs(self):
        job_id = self.manager.add_job(
            url="https://aebn.net/movie",
            threads=5,
            resolution="720",
            scene="1",
            output_dir="./test_downloads"
        )
        for i in range(50):
            self.manager.active_jobs[job_id]["logs"].append(f"Verbose log line {i}")
        
        status_list = self.manager.get_status()
        self.assertTrue(len(status_list) > 0)
        matching_job = next(j for j in status_list if j["id"] == job_id)
        self.assertNotIn("logs", matching_job, "Sanitized status must exclude raw logs to keep SSE lightweight")
        self.assertNotIn("process_obj", matching_job)


class TestInputValidationAndAuth(unittest.TestCase):
    def test_optional_auth_disabled_by_default(self):
        # When AUTH_USERNAME and AUTH_PASSWORD are not set in environment
        with patch.dict(os.environ, {}, clear=True):
            from src import main
            # Verify verify_auth passes without credentials
            result = asyncio.run(verify_auth(None))
            self.assertTrue(result)

    def test_auth_enforced_when_configured(self):
        from fastapi.security import HTTPBasicCredentials
        from src import main
        
        with patch.object(main, "AUTH_ENABLED", True), \
             patch.object(main, "AUTH_USERNAME", "admin"), \
             patch.object(main, "AUTH_PASSWORD", "secret123"):
            
            # Missing credentials -> 401
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(main.verify_auth(None))
            self.assertEqual(cm.exception.status_code, 401)
            
            # Invalid credentials -> 401
            bad_creds = HTTPBasicCredentials(username="admin", password="wrongpassword")
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(main.verify_auth(bad_creds))
            self.assertEqual(cm.exception.status_code, 401)
            
            # Valid credentials -> True
            good_creds = HTTPBasicCredentials(username="admin", password="secret123")
            self.assertTrue(asyncio.run(main.verify_auth(good_creds)))

    def test_download_endpoint_validation(self):
        from src.main import download

        # 1. Invalid URL scheme
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(download(url="ftp://example.com/test", scene="1", threads=10, resolution="720", _=True))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Must start with http:// or https://", cm.exception.detail)

        # 2. Flag-like URL argument injection attempt
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(download(url="--proxy http://bad.com", scene="1", threads=10, resolution="720", _=True))
        self.assertEqual(cm.exception.status_code, 400)

        # 3. Invalid non-numeric scene
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(download(url="https://aebn.net/test", scene="invalid_scene", threads=10, resolution="720", _=True))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Scene must be a valid positive number", cm.exception.detail)

        # 4. Valid inputs queue correctly
        res = asyncio.run(download(
            url="https://aebn.net/test-movie",
            scene="3",
            threads=100,  # Should be clamped
            resolution="2160",
            names=True,
            covers=True,
            split_scenes=False,
            _=True
        ))
        self.assertEqual(res["status"], "queued")
        self.assertRegex(res["job_id"], r"^[a-f0-9]{8}$")


class TestASGIRoutesAndSecurityHeaders(unittest.TestCase):
    async def _make_request(self, method, path, headers=None, body=b""):
        req_headers = [[b'host', b'localhost']]
        if headers:
            for k, v in headers.items():
                req_headers.append([k.lower().encode(), v.encode()])

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': method,
            'path': path,
            'query_string': b'',
            'headers': req_headers
        }
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(msg):
            sent.append(msg)

        await app(scope, receive, send)
        status_code = sent[0]['status']
        resp_headers = dict(sent[0]['headers'])
        resp_body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
        return status_code, resp_headers, resp_body

    def test_security_headers_present(self):
        status_code, headers, body = asyncio.run(self._make_request('GET', '/system-info'))
        self.assertEqual(status_code, 200)
        self.assertEqual(headers.get(b'x-content-type-options'), b'nosniff')
        self.assertEqual(headers.get(b'x-frame-options'), b'DENY')
        self.assertEqual(headers.get(b'referrer-policy'), b'strict-origin-when-cross-origin')

    def test_invalid_log_request_rejected(self):
        # Invalid format job_id in /logs/{job_id} -> 400
        status_code, headers, body = asyncio.run(self._make_request('GET', '/logs/invalid-id-too-long'))
        self.assertEqual(status_code, 400)


if __name__ == "__main__":
    unittest.main()

