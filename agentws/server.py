#!/usr/bin/env python3
"""Small AgentWS job server.

GitHub owns tasks. The server owns jobs. A GitHub issue webhook creates the
first planner job, and local agents use the job API for job creation and job
lifecycle while keeping task/repository operations in gh/git.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


VALID_STATUSES = {"pending", "claimed", "running", "done", "failed"}
TERMINAL_STATUSES = {"done", "failed"}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def compact_name(value: str) -> str:
    out = []
    for char in value:
        if char.isalnum() or char in "._-":
            out.append(char)
        else:
            out.append("-")
    text = "".join(out).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    return text or "task"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_id TEXT,
                    spec TEXT NOT NULL,
                    source TEXT,
                    source_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_status_role_idx
                    ON jobs(status, role, created_at);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    agent_id TEXT,
                    message TEXT,
                    detail TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS github_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    action TEXT,
                    job_id TEXT
                );
                """
            )

    def create_job(
        self,
        job_id: str,
        task_id: str,
        role: str,
        spec: str,
        *,
        source: str | None = None,
        source_url: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        ts = now()
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO jobs
                        (id, task_id, role, status, spec, source, source_url,
                         created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (job_id, task_id, role, spec, source, source_url, ts, ts),
                )
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return False, row_to_job(row, conn)

            conn.execute(
                """
                INSERT INTO job_events (job_id, ts, kind, message, detail)
                VALUES (?, ?, 'created', ?, ?)
                """,
                (job_id, ts, f"Created {role} job", source_url or ""),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return True, row_to_job(row, conn)

    def list_jobs(self, status: str | None = None, role: str | None = None) -> list[dict[str, Any]]:
        where = []
        params: list[str] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if role:
            where.append("role = ?")
            params.append(role)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {clause} ORDER BY created_at, id",
                params,
            ).fetchall()
            return [row_to_job(row, conn, include_events=False) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            return row_to_job(row, conn)

    def claim_job(
        self,
        role: str | None,
        agent_id: str,
        *,
        job_id: str | None = None,
    ) -> dict[str, Any] | None:
        ts = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            where = ["status = 'pending'"]
            params: list[str] = []
            if role:
                where.append("role = ?")
                params.append(role)
            if job_id:
                where.append("id = ?")
                params.append(job_id)
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(where)}
                ORDER BY created_at, id
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            job_id = row["id"]
            conn.execute(
                """
                UPDATE jobs
                SET status = 'claimed', agent_id = ?, claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (agent_id, ts, ts, job_id),
            )
            conn.execute(
                """
                INSERT INTO job_events (job_id, ts, kind, agent_id, message)
                VALUES (?, ?, 'claimed', ?, ?)
                """,
                (job_id, ts, agent_id, f"Claimed by {agent_id}"),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            conn.commit()
            return row_to_job(row, conn)

    def transition(
        self,
        job_id: str,
        agent_id: str,
        target: str,
        message: str,
    ) -> dict[str, Any]:
        if target not in {"running", "done", "failed", "pending"}:
            raise ValueError(f"invalid target status: {target}")
        ts = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            require_owner(row, agent_id)
            current = row["status"]

            if target == "running" and current != "claimed":
                raise ValueError(f"cannot start job from {current}")
            if target == "done" and current != "running":
                raise ValueError(f"cannot finish job from {current}")
            if target == "failed" and current not in {"claimed", "running"}:
                raise ValueError(f"cannot fail job from {current}")
            if target == "pending" and current not in {"claimed", "running"}:
                raise ValueError(f"cannot release job from {current}")

            if target == "pending":
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending', agent_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (ts, job_id),
                )
                event = "released"
            elif target == "running":
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ts, ts, job_id),
                )
                event = "started"
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target, ts, ts, job_id),
                )
                event = target

            conn.execute(
                """
                INSERT INTO job_events (job_id, ts, kind, agent_id, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, ts, event, agent_id, message),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            conn.commit()
            return row_to_job(row, conn)

    def append_log(
        self,
        job_id: str,
        agent_id: str | None,
        summary: str,
        detail: str,
    ) -> dict[str, Any]:
        ts = now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if agent_id:
                require_owner(row, agent_id)
            conn.execute(
                """
                INSERT INTO job_events (job_id, ts, kind, agent_id, message, detail)
                VALUES (?, ?, 'log', ?, ?, ?)
                """,
                (job_id, ts, agent_id, summary, detail),
            )
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts, job_id))
            return row_to_job(row, conn)

    def record_delivery(
        self,
        delivery_id: str,
        event: str,
        action: str | None,
        job_id: str | None,
    ) -> bool:
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO github_deliveries
                        (delivery_id, received_at, event, action, job_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (delivery_id, now(), event, action, job_id),
                )
                return True
            except sqlite3.IntegrityError:
                return False


def row_to_job(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    include_events: bool = True,
) -> dict[str, Any]:
    job = {key: row[key] for key in row.keys()}
    if include_events:
        events = conn.execute(
            """
            SELECT ts, kind, agent_id, message, detail
            FROM job_events
            WHERE job_id = ?
            ORDER BY id
            """,
            (row["id"],),
        ).fetchall()
        job["events"] = [{key: event[key] for key in event.keys()} for event in events]
    return job


def require_owner(row: sqlite3.Row, agent_id: str) -> None:
    owner = row["agent_id"]
    if owner != agent_id:
        raise PermissionError(f"job owned by {owner!r}, not {agent_id!r}")


class App:
    def __init__(
        self,
        store: Store,
        *,
        api_token: str | None = None,
        webhook_secret: str | None = None,
        require_label: str | None = None,
    ) -> None:
        self.store = store
        self.api_token = api_token
        self.webhook_secret = webhook_secret
        self.require_label = require_label


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentWSServer/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:
        try:
            self.route_post()
        except Exception as exc:
            self.handle_error(exc)

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/health":
            self.send_json({"ok": True, "time": now()})
            return
        self.require_api_auth()
        if path == "/jobs":
            status = first(query.get("status"))
            role = first(query.get("role"))
            if status and status not in VALID_STATUSES:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"invalid status: {status}")
                return
            self.send_json({"jobs": self.app.store.list_jobs(status=status, role=role)})
            return
        if path.startswith("/jobs/"):
            job_id = path.removeprefix("/jobs/")
            job = self.app.store.get_job(job_id)
            if job is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "job not found")
                return
            self.send_json({"job": job})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def route_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/github/webhook":
            self.handle_github_webhook()
            return
        self.require_api_auth()
        if path == "/jobs":
            body = self.read_json()
            created, job = self.app.store.create_job(
                require_text(body, "id"),
                require_text(body, "task_id"),
                require_text(body, "role"),
                require_text(body, "spec"),
                source=text_or_none(body.get("source")),
                source_url=text_or_none(body.get("source_url")),
            )
            self.send_json({"created": created, "job": job}, HTTPStatus.CREATED if created else HTTPStatus.OK)
            return
        if path == "/jobs/claim":
            body = self.read_json()
            job = self.app.store.claim_job(
                text_or_none(body.get("role")),
                require_text(body, "agent_id"),
            )
            if job is None:
                self.send_json({"job": None}, HTTPStatus.NO_CONTENT)
            else:
                self.send_json({"job": job})
            return
        if path.startswith("/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            _, job_id, action = parts
            body = self.read_json()
            agent_id = require_text(body, "agent_id") if action not in {"log"} else text_or_none(body.get("agent_id"))
            if action == "claim":
                job = self.app.store.claim_job(
                    text_or_none(body.get("role")),
                    agent_id or "",
                    job_id=job_id,
                )
                if job is None:
                    self.send_json({"job": None}, HTTPStatus.NO_CONTENT)
                    return
            elif action == "start":
                job = self.app.store.transition(job_id, agent_id or "", "running", text_or_none(body.get("message")) or "Started")
            elif action == "done":
                job = self.app.store.transition(job_id, agent_id or "", "done", require_text(body, "message"))
            elif action == "fail":
                job = self.app.store.transition(job_id, agent_id or "", "failed", require_text(body, "message"))
            elif action == "release":
                job = self.app.store.transition(job_id, agent_id or "", "pending", require_text(body, "message"))
            elif action == "log":
                job = self.app.store.append_log(
                    job_id,
                    agent_id,
                    require_text(body, "summary"),
                    text_or_none(body.get("detail")) or "",
                )
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            self.send_json({"job": job})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def handle_github_webhook(self) -> None:
        raw = self.read_body()
        self.verify_webhook_signature(raw)
        event = self.headers.get("X-GitHub-Event", "")
        delivery = self.headers.get("X-GitHub-Delivery", "")
        payload = json.loads(raw.decode("utf-8") or "{}")
        action = payload.get("action")

        if event != "issues":
            self.record_delivery_if_present(delivery, event, action, None)
            self.send_json({"accepted": False, "reason": "ignored event"})
            return

        issue = payload.get("issue") or {}
        repo = payload.get("repository") or {}
        if issue.get("pull_request"):
            self.record_delivery_if_present(delivery, event, action, None)
            self.send_json({"accepted": False, "reason": "ignored pull request issue"})
            return
        if action not in {"opened", "reopened", "labeled"}:
            self.record_delivery_if_present(delivery, event, action, None)
            self.send_json({"accepted": False, "reason": "ignored action"})
            return
        if self.app.require_label and not issue_has_label(issue, self.app.require_label):
            self.record_delivery_if_present(delivery, event, action, None)
            self.send_json({"accepted": False, "reason": "missing required label"})
            return

        repo_full = repo.get("full_name") or "unknown/repo"
        number = str(issue.get("number"))
        job_id = f"{compact_name(repo_full)}-{number}-plan"
        task_id = number
        spec = planner_spec(repo_full, issue)

        if delivery and not self.app.store.record_delivery(delivery, event, action, job_id):
            self.send_json({"accepted": False, "reason": "duplicate delivery", "job_id": job_id})
            return

        created, job = self.app.store.create_job(
            job_id,
            task_id,
            "planner",
            spec,
            source=f"github:{repo_full}#{number}",
            source_url=issue.get("html_url"),
        )
        self.send_json({"accepted": True, "created": created, "job": job})

    def record_delivery_if_present(
        self,
        delivery: str,
        event: str,
        action: str | None,
        job_id: str | None,
    ) -> None:
        if delivery:
            self.app.store.record_delivery(delivery, event, action, job_id)

    def verify_webhook_signature(self, raw: bytes) -> None:
        secret = self.app.webhook_secret
        if not secret:
            return
        header = self.headers.get("X-Hub-Signature-256", "")
        prefix = "sha256="
        if not header.startswith(prefix):
            raise PermissionError("missing X-Hub-Signature-256")
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(header[len(prefix):], expected):
            raise PermissionError("invalid webhook signature")

    def require_api_auth(self) -> None:
        token = self.app.api_token
        if not token:
            return
        expected = f"Bearer {token}"
        if self.headers.get("Authorization") != expected:
            raise PermissionError("invalid API token")

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def read_json(self) -> dict[str, Any]:
        raw = self.read_body()
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        if status == HTTPStatus.NO_CONTENT:
            self.send_response(status)
            self.end_headers()
            return
        raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status)

    def handle_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
        elif isinstance(exc, KeyError):
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        elif isinstance(exc, (ValueError, json.JSONDecodeError)):
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            print(f"server error: {exc}", file=sys.stderr)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def planner_spec(repo_full: str, issue: dict[str, Any]) -> str:
    number = issue.get("number")
    url = issue.get("html_url") or ""
    return f"""# Plan for task {number}

## Task

Task id: {number}
Repository: {repo_full}
Source: {url}

## Objective
Read the task through the AgentWS task tool:

```sh
bin/task-show {number}
```

Mark the task accepted/running with:

```sh
bin/task-state {number} open
```

Then create the jobs needed to satisfy the task. Use only AgentWS tools for
task and job state.

## When Done
Create follow-up jobs with the remote job API, then mark this planner job done.
The task is complete only when planner decides the work is complete and records
the result with `bin/task-result`.
"""


def issue_has_label(issue: dict[str, Any], label: str) -> bool:
    for item in issue.get("labels") or []:
        if isinstance(item, dict) and item.get("name") == label:
            return True
    return False


def first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected string value")
    return value


def require_text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing required string field: {key}")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the AgentWS job server")
    parser.add_argument("--host", default=os.environ.get("AGENTWS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENTWS_PORT", "8765")))
    parser.add_argument(
        "--db",
        default=os.environ.get("AGENTWS_DB", "agentws-server.sqlite3"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--require-label",
        default=os.environ.get("AGENTWS_GITHUB_REQUIRE_LABEL"),
        help="Only create jobs for issues with this label",
    )
    args = parser.parse_args(argv)

    app = App(
        Store(Path(args.db)),
        api_token=os.environ.get("AGENTWS_API_TOKEN"),
        webhook_secret=os.environ.get("AGENTWS_GITHUB_SECRET"),
        require_label=args.require_label,
    )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.app = app  # type: ignore[attr-defined]
    print(f"AgentWS job server listening on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"database: {Path(args.db).resolve()}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
