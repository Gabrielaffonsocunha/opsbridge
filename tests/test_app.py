import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class OpsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "opsbridge.db")
        self.token = "test-token-only"
        self.client = TestClient(create_app(db_path=db_path, webhook_token=self.token))
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_repeated_alert_is_deduplicated(self):
        alert = {
            "labels": {"alertname": "EndpointDown", "instance": "api-demo:8080", "severity": "critical"},
            "annotations": {"summary": "API de demonstração indisponível"},
            "fingerprint": "demo-fp-1",
            "status": "firing",
        }
        first = self.client.post("/webhooks/alertmanager", headers=self.headers, json={"alerts": [alert]})
        second = self.client.post("/webhooks/alertmanager", headers=self.headers, json={"alerts": [alert]})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["results"][0]["result"], "created")
        self.assertEqual(second.json()["results"][0]["result"], "deduplicated")
        self.assertEqual(len(self.client.get("/api/incidents", headers=self.headers).json()["incidents"]), 1)

    def test_resolved_alert_updates_incident(self):
        firing = {"status": "firing", "fingerprint": "demo-fp-2", "alert_name": "DiskHigh", "instance": "host-1"}
        resolved = {**firing, "status": "resolved"}
        self.client.post("/webhooks/zabbix", headers=self.headers, json=firing)
        result = self.client.post("/webhooks/zabbix", headers=self.headers, json=resolved)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["result"], "resolved")
        incidents = self.client.get("/api/incidents?status=resolved", headers=self.headers).json()["incidents"]
        self.assertEqual(len(incidents), 1)

    def test_invalid_token_is_rejected(self):
        response = self.client.get("/api/incidents", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)

    def test_invalid_alert_status_is_rejected(self):
        response = self.client.post(
            "/webhooks/zabbix", headers=self.headers,
            json={"status": "unknown", "alert_name": "Test", "instance": "host-1"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
