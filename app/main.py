"""OpsBridge: receive monitoring alerts and track them as incidents."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with session(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                alert_name TEXT NOT NULL,
                instance TEXT NOT NULL,
                severity TEXT NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS incident_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                event_type TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
            CREATE INDEX IF NOT EXISTS idx_incidents_last_seen ON incidents(last_seen DESC);
            """
        )


def normalize_alert(raw: dict[str, Any], source: str) -> dict[str, str]:
    """Accept an Alertmanager alert or the small OpsBridge/Zabbix demo format."""
    labels = raw.get("labels") or {}
    annotations = raw.get("annotations") or {}
    alert_name = str(labels.get("alertname") or raw.get("alert_name") or "UnknownAlert").strip()
    instance = str(labels.get("instance") or raw.get("instance") or "unknown").strip()
    severity = str(labels.get("severity") or raw.get("severity") or "warning").lower().strip()
    summary = str(
        annotations.get("summary")
        or annotations.get("description")
        or raw.get("summary")
        or raw.get("message")
        or alert_name
    ).strip()
    status = str(raw.get("status") or "firing").lower().strip()
    fingerprint = str(raw.get("fingerprint") or f"{source}:{alert_name}:{instance}").strip()

    if not alert_name or not instance or not fingerprint:
        raise ValueError("alert_name, instance e fingerprint não podem ficar vazios")
    if status not in {"firing", "resolved"}:
        raise ValueError("status deve ser 'firing' ou 'resolved'")
    if severity not in {"critical", "high", "warning", "info"}:
        severity = "warning"

    return {
        "fingerprint": fingerprint[:255],
        "source": source[:80],
        "alert_name": alert_name[:160],
        "instance": instance[:255],
        "severity": severity,
        "summary": summary[:1000],
        "status": status,
    }


def record_alert(db_path: str, alert: dict[str, str]) -> tuple[dict[str, Any], str]:
    timestamp = now_iso()
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ?", (alert["fingerprint"],)
        ).fetchone()

        if alert["status"] == "resolved":
            if row is None:
                return {"incident": None, "result": "ignored_unmatched_resolution"}, "ignored"
            conn.execute(
                "UPDATE incidents SET status='resolved', last_seen=?, resolved_at=? WHERE id=?",
                (timestamp, timestamp, row["id"]),
            )
            event_type = "resolved" if row["status"] != "resolved" else "resolution_repeated"
            conn.execute(
                "INSERT INTO incident_events (incident_id,event_type,details,created_at) VALUES (?,?,?,?)",
                (row["id"], event_type, alert["summary"], timestamp),
            )
            updated = conn.execute("SELECT * FROM incidents WHERE id=?", (row["id"],)).fetchone()
            return {"incident": dict(updated), "result": event_type}, event_type

        if row is None:
            cur = conn.execute(
                """INSERT INTO incidents
                (fingerprint,source,alert_name,instance,severity,summary,status,first_seen,last_seen)
                VALUES (?,?,?,?,?,?,'open',?,?)""",
                (
                    alert["fingerprint"], alert["source"], alert["alert_name"],
                    alert["instance"], alert["severity"], alert["summary"], timestamp, timestamp,
                ),
            )
            incident_id = cur.lastrowid
            event_type = "opened"
            result = "created"
        else:
            incident_id = row["id"]
            event_type = "reopened" if row["status"] == "resolved" else "duplicate_suppressed"
            result = "reopened" if row["status"] == "resolved" else "deduplicated"
            conn.execute(
                """UPDATE incidents SET source=?,alert_name=?,instance=?,severity=?,summary=?,
                   status='open',last_seen=?,resolved_at=NULL WHERE id=?""",
                (
                    alert["source"], alert["alert_name"], alert["instance"], alert["severity"],
                    alert["summary"], timestamp, incident_id,
                ),
            )

        conn.execute(
            "INSERT INTO incident_events (incident_id,event_type,details,created_at) VALUES (?,?,?,?)",
            (incident_id, event_type, alert["summary"], timestamp),
        )
        saved = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return {"incident": dict(saved), "result": result}, result


def create_app(db_path: str | None = None, webhook_token: str | None = None) -> FastAPI:
    database = db_path or os.getenv("OPSBRIDGE_DB_PATH", "/data/opsbridge.db")
    token = webhook_token if webhook_token is not None else os.getenv("OPSBRIDGE_WEBHOOK_TOKEN", "")
    initialize(database)
    app = FastAPI(title="OpsBridge API", version="0.1.0", description="Monitoring alerts to incidents")
    app.state.db_path = database
    app.state.webhook_token = token
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    def authorize(authorization: str | None) -> None:
        expected = app.state.webhook_token
        if not expected:
            raise HTTPException(status_code=503, detail="Configure OPSBRIDGE_WEBHOOK_TOKEN")
        provided = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Token ausente ou inválido")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/alertmanager")
    async def alertmanager(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON inválido") from exc
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list):
            raise HTTPException(status_code=422, detail="Esperado um array 'alerts'")
        results = []
        for raw in alerts:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422, detail="Cada alerta deve ser um objeto JSON")
            try:
                normalized = normalize_alert(raw, "alertmanager")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            result, _ = record_alert(database, normalized)
            results.append(result)
        return {"processed": len(results), "results": results}

    @app.post("/webhooks/zabbix")
    async def zabbix(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        try:
            raw = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON inválido") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Esperado um objeto JSON")
        try:
            normalized = normalize_alert(raw, "zabbix")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result, _ = record_alert(database, normalized)
        return result

    @app.get("/api/incidents")
    def list_incidents(
        status: str = "all", authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(authorization)
        if status not in {"all", "open", "resolved"}:
            raise HTTPException(status_code=422, detail="status deve ser all, open ou resolved")
        with session(database) as conn:
            query = "SELECT * FROM incidents"
            args: tuple[str, ...] = ()
            if status != "all":
                query += " WHERE status=?"
                args = (status,)
            query += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, last_seen DESC"
            rows = conn.execute(query, args).fetchall()
            return {"incidents": [dict(row) for row in rows]}

    @app.get("/api/incidents/{incident_id}/events")
    def list_events(incident_id: int, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        with session(database) as conn:
            exists = conn.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Incidente não encontrado")
            rows = conn.execute(
                "SELECT id,event_type,details,created_at FROM incident_events WHERE incident_id=? ORDER BY id DESC",
                (incident_id,),
            ).fetchall()
            return {"events": [dict(row) for row in rows]}

    @app.patch("/api/incidents/{incident_id}/resolve")
    def resolve_incident(incident_id: int, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        timestamp = now_iso()
        with session(database) as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Incidente não encontrado")
            if row["status"] == "open":
                conn.execute(
                    "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?", (timestamp, incident_id)
                )
                conn.execute(
                    "INSERT INTO incident_events (incident_id,event_type,details,created_at) VALUES (?,?,?,?)",
                    (incident_id, "manually_resolved", "Resolução manual pelo painel", timestamp),
                )
            updated = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            return {"incident": dict(updated)}

    return app


app = create_app()
