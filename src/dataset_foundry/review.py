"""Local-only human review UI backed by a sidecar SQLite database.

Review decisions deliberately do not modify collection or processing manifests.  This
module has no CLI integration so callers can choose how to provision and disclose the
bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote_to_bytes, urlsplit

from .manifest import read_jsonl

DECISIONS = frozenset({"accepted", "rejected", "pending"})
MAX_REQUEST_BYTES = 16 * 1024
MAX_NOTE_LENGTH = 4_000


class AssetIntegrityError(ValueError):
    """A manifest-whitelisted asset is missing, unsafe, or has changed."""


class ReviewStore:
    """Read candidates from a run and persist decisions outside its manifests."""

    def __init__(self, run_dir: Path, database_path: Path | None = None):
        self.run_dir = run_dir.resolve()
        manifest_path = self.run_dir / "processed.jsonl"
        if not manifest_path.exists():
            manifest_path = self.run_dir / "manifest.jsonl"
        records = read_jsonl(manifest_path)

        self._records: dict[str, dict] = {}
        for record in records:
            if record.get("status") not in {"downloaded", "derived"}:
                continue
            asset_id = record.get("asset_id")
            relative = record.get("file_path")
            if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 256:
                raise ValueError("review manifest contains an invalid asset ID")
            if asset_id in self._records:
                raise ValueError(f"review manifest contains duplicate asset ID: {asset_id}")
            if not isinstance(relative, str) or not relative:
                raise ValueError(f"review asset has no file path: {asset_id}")
            self._records[asset_id] = dict(record)

        self.database_path = (
            database_path.resolve()
            if database_path is not None
            else self.run_dir / "review.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_decisions (
                        asset_id TEXT PRIMARY KEY,
                        decision TEXT NOT NULL CHECK (
                            decision IN ('accepted', 'rejected', 'pending')
                        ),
                        note TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    )
                    """
            )

    def _decision_rows(self) -> dict[str, dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT asset_id, decision, note, updated_at FROM review_decisions"
            ).fetchall()
        return {
            row["asset_id"]: {
                "decision": row["decision"],
                "note": row["note"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def candidates(self) -> list[dict]:
        decisions = self._decision_rows()
        result = []
        exposed_fields = (
            "asset_id",
            "parent_id",
            "source_id",
            "source_adapter",
            "origin",
            "original_name",
            "license",
            "rights_note",
            "mime_guess",
            "mime_detected",
            "mime_declared",
            "mime_validation",
            "media_kind",
            "bytes",
            "sha256",
            "status",
            "attempts",
        )
        for asset_id, record in self._records.items():
            item = {key: record.get(key) for key in exposed_fields}
            item["decision"] = decisions.get(
                asset_id,
                {"decision": "pending", "note": "", "updated_at": None},
            )
            result.append(item)
        return result

    def set_decision(self, asset_id: str, decision: str, note: str = "") -> dict:
        if asset_id not in self._records:
            raise KeyError(asset_id)
        if decision not in DECISIONS:
            raise ValueError("decision must be accepted, rejected, or pending")
        if not isinstance(note, str):
            raise TypeError("note must be a string")
        if len(note) > MAX_NOTE_LENGTH:
            raise ValueError(f"note exceeds {MAX_NOTE_LENGTH} characters")
        updated_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                    """
                    INSERT INTO review_decisions(asset_id, decision, note, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        decision = excluded.decision,
                        note = excluded.note,
                        updated_at = excluded.updated_at
                    """,
                    (asset_id, decision, note, updated_at),
            )
        return {
            "asset_id": asset_id,
            "decision": decision,
            "note": note,
            "updated_at": updated_at,
        }

    def open_verified_asset(self, asset_id: str) -> tuple[BinaryIO, dict]:
        """Open one whitelisted asset and verify the same file handle before serving."""
        try:
            record = self._records[asset_id]
        except KeyError:
            raise KeyError(asset_id) from None

        relative = record["file_path"]
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise AssetIntegrityError("asset path is not relative")
        try:
            asset = (self.run_dir / relative_path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AssetIntegrityError("asset is missing or inaccessible") from exc
        if not asset.is_relative_to(self.run_dir) or not asset.is_file():
            raise AssetIntegrityError("asset path escapes the run directory")

        expected_size = record.get("bytes")
        expected_digest = record.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not isinstance(expected_digest, str)
            or len(expected_digest) != 64
        ):
            raise AssetIntegrityError("asset has invalid integrity metadata")

        handle = asset.open("rb")
        try:
            if os.fstat(handle.fileno()).st_size != expected_size:
                raise AssetIntegrityError("asset size does not match the manifest")
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), expected_digest.lower()):
                raise AssetIntegrityError("asset checksum does not match the manifest")
            handle.seek(0)
            return handle, record
        except Exception:
            handle.close()
            raise


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, store: ReviewStore, token: str):
        self.store = store
        self.review_token = token
        super().__init__(server_address, handler_class)


def _decode_asset_id(path: str, prefix: str) -> str | None:
    if not path.startswith(prefix):
        return None
    encoded = path[len(prefix) :]
    if not encoded:
        return None
    try:
        return unquote_to_bytes(encoded).decode("utf-8")
    except UnicodeDecodeError:
        return None


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer
    server_version = "DatasetFoundryReview/0.3"
    sys_version = ""

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send_json(self, status: HTTPStatus, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.review_token}"
        if hmac.compare_digest(supplied, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self._common_headers()
        self.send_header("WWW-Authenticate", 'Bearer realm="dataset-foundry-review"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._serve_index()
            return
        if path == "/api/candidates":
            if self._authorized():
                self._send_json(HTTPStatus.OK, self.server.store.candidates())
            return
        asset_id = _decode_asset_id(path, "/api/assets/")
        if asset_id is not None:
            if self._authorized():
                self._serve_asset(asset_id)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        asset_id = _decode_asset_id(path, "/api/decisions/")
        if asset_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        if not 0 <= length <= MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
            return
        try:
            result = self.server.store.set_decision(
                asset_id,
                payload.get("decision"),
                payload.get("note", ""),
            )
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown asset"})
        except (TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self._send_json(HTTPStatus.OK, result)

    def _serve_asset(self, asset_id: str) -> None:
        try:
            handle, record = self.server.store.open_verified_asset(asset_id)
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown asset"})
            return
        except AssetIntegrityError:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "asset integrity verification failed"},
            )
            return
        try:
            content_type = mimetypes.guess_type(record.get("original_name", ""))[0]
            self.send_response(HTTPStatus.OK)
            self._common_headers()
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(record["bytes"]))
            self.send_header("Content-Disposition", 'inline; filename="asset"')
            self.end_headers()
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                self.wfile.write(chunk)
        finally:
            handle.close()

    def _serve_index(self) -> None:
        nonce = secrets.token_urlsafe(18)
        body = _INDEX_HTML.replace("__NONCE__", nonce).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src blob:; media-src blob:; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Avoid copying asset IDs, notes, or accidental query parameters into logs.
        return


def create_review_server(
    run_dir: Path,
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    database_path: Path | None = None,
) -> ReviewHTTPServer:
    """Build a token-protected server restricted to an IPv4 loopback address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("review server host must be a literal loopback IP") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("review server may bind only to an IPv4 loopback address")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if not isinstance(token, str) or not token:
        raise ValueError("a non-empty review token is required")
    store = ReviewStore(run_dir, database_path)
    return ReviewHTTPServer((host, port), ReviewRequestHandler, store, token)


def serve_review(
    run_dir: Path,
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    database_path: Path | None = None,
) -> None:
    """Serve until interrupted. Use :func:`create_review_server` for embedding/tests."""
    server = create_review_server(
        run_dir,
        token,
        host=host,
        port=port,
        database_path=database_path,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def generate_review_token() -> str:
    """Return a high-entropy token suitable for one local review session."""
    return secrets.token_urlsafe(32)


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Dataset Foundry Review</title>
<style nonce="__NONCE__">
body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
button,textarea,input{font:inherit;margin:.25rem;padding:.5rem}button{cursor:pointer}.bar{display:flex;gap:.5rem;flex-wrap:wrap}
#asset img,#asset video{max-width:100%;max-height:60vh}pre{white-space:pre-wrap}textarea{width:100%;box-sizing:border-box}
</style></head><body><h1>Dataset Foundry Review</h1>
<div id="login"><input id="token" type="password" autocomplete="off" placeholder="Review token"><button id="open">Open</button></div>
<main id="review" hidden><div class="bar"><button id="prev">Previous</button><button id="next">Next</button><span id="position"></span></div>
<h2 id="name"></h2><div id="asset"></div><pre id="meta"></pre><textarea id="note" maxlength="4000" placeholder="Review note"></textarea>
<div class="bar"><button data-decision="accepted">Accept</button><button data-decision="rejected">Reject</button><button data-decision="pending">Pending</button></div></main>
<p id="message"></p>
<script nonce="__NONCE__">
'use strict';let token='',items=[],index=0,objectUrl='';const $=id=>document.getElementById(id);
async function api(path,options={}){options.headers={...(options.headers||{}),Authorization:'Bearer '+token};const r=await fetch(path,options);if(!r.ok)throw new Error((await r.json().catch(()=>({error:r.statusText}))).error);return r}
async function openReview(){token=$('token').value;try{items=await (await api('/api/candidates')).json();$('login').hidden=true;$('review').hidden=false;await render()}catch(e){$('message').textContent=e.message}}
async function render(){if(objectUrl)URL.revokeObjectURL(objectUrl);$('asset').replaceChildren();if(!items.length){$('message').textContent='No reviewable assets.';return}const x=items[index];$('position').textContent=`${index+1} / ${items.length}`;$('name').textContent=x.original_name||x.asset_id;$('meta').textContent=JSON.stringify(x,null,2);$('note').value=x.decision.note||'';try{const blob=await (await api('/api/assets/'+encodeURIComponent(x.asset_id))).blob();objectUrl=URL.createObjectURL(blob);let el;if((x.media_kind||'')==='image')el=new Image();else if(['video','audio'].includes(x.media_kind)){el=document.createElement(x.media_kind);el.controls=true}else{el=document.createElement('a');el.textContent='Open asset';el.download=x.original_name||'asset'}el.src=objectUrl;$('asset').append(el)}catch(e){$('message').textContent=e.message}}
async function decide(value){const x=items[index];try{x.decision=await (await api('/api/decisions/'+encodeURIComponent(x.asset_id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:value,note:$('note').value})})).json();$('message').textContent='Saved: '+value;await render()}catch(e){$('message').textContent=e.message}}
$('open').onclick=openReview;$('prev').onclick=()=>{index=(index-1+items.length)%items.length;render()};$('next').onclick=()=>{index=(index+1)%items.length;render()};document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=()=>decide(b.dataset.decision));
</script></body></html>"""
