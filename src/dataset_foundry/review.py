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
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dataset Foundry Review</title>
<style nonce="__NONCE__">
:root{--bg:#f1f5f9;--card:#ffffff;--text:#0f172a;--muted:#64748b;--border:#cbd5e1;--primary:#2563eb;--green:#16a34a;--green-hover:#15803d;--red:#dc2626;--red-hover:#b91c1c;--amber:#d97706;--amber-hover:#b45309}
*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none !important}
html,body{height:100vh;overflow:hidden;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.container{display:flex;flex-direction:column;height:100vh;max-width:1200px;margin:0 auto;padding:.5rem 1rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;box-shadow:0 2px 4px rgba(0,0,0,.04)}
#login{max-width:440px;margin:15vh auto;padding:1.5rem;display:flex;flex-direction:column;gap:.75rem}
#login input{padding:.625rem .75rem;border:1px solid var(--border);border-radius:6px;font:inherit;background:#fff}
#login button{padding:.625rem;background:var(--primary);color:#fff;border:none;border-radius:6px;font:inherit;font-weight:600;cursor:pointer}
#review{display:flex;flex-direction:column;flex:1;min-height:0;padding:.75rem 1rem;margin:0}
header{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding-bottom:.5rem;border-bottom:1px solid var(--border);margin-bottom:.5rem;flex-shrink:0}
.header-left{display:flex;align-items:baseline;gap:.75rem}
h1{font-size:1.15rem;font-weight:700}
.asset-title{font-size:13px;font-weight:600;color:var(--muted);word-break:break-all}
.header-right{display:flex;align-items:center;gap:.5rem}
.badge{padding:.2rem .5rem;font-size:11px;font-weight:700;border-radius:9999px;text-transform:uppercase}
.badge-pending{background:#fef3c7;color:#92400e}
.badge-accepted{background:#dcfce7;color:#166534}
.badge-rejected{background:#fee2e2;color:#991b1b}
.stat-pill{padding:2px 8px;border-radius:4px;background:#f8fafc;border:1px solid var(--border);font-size:12px;font-weight:600}
.filter-bar{display:flex;align-items:center;gap:.35rem;margin-bottom:.5rem;flex-shrink:0}
.filter-btn{font-size:12px;padding:.2rem .6rem;border:1px solid var(--border);background:#fff;border-radius:4px;cursor:pointer;color:var(--muted);font-weight:500}
.filter-btn.active{background:var(--primary);color:#fff;border-color:var(--primary);font-weight:600}
#asset-container{flex:1;min-height:0;background:#090d16;border-radius:8px;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative;margin-bottom:.5rem}
#asset{width:100%;height:100%;display:flex;align-items:center;justify-content:center}
#asset img,#asset video{max-width:100%;max-height:100%;object-fit:contain;display:block}
#asset a{color:#38bdf8;padding:2rem;text-decoration:none}
details.meta-box{flex-shrink:0;background:#f8fafc;border:1px solid var(--border);border-radius:6px;margin-bottom:.5rem}
summary.meta-summary{padding:.35rem .6rem;cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);user-select:none;background:#f1f5f9;outline:none}
summary.meta-summary:hover{background:#e2e8f0;color:var(--text)}
#meta{padding:.5rem;font-size:11px;line-height:1.35;max-height:120px;overflow-y:auto;white-space:pre-wrap;color:#334155;border-top:1px solid var(--border)}
.dock{flex-shrink:0;display:flex;flex-direction:column;gap:.4rem;background:#ffffff;border-top:1px solid var(--border);padding-top:.5rem}
#note{width:100%;padding:.4rem .6rem;font:inherit;font-size:13px;border:1px solid var(--border);border-radius:6px;height:38px;resize:none;background:#f8fafc}
#note:focus{background:#fff;border-color:var(--primary);outline:none;box-shadow:0 0 0 2px rgba(37,99,235,.15)}
.actions-row{display:grid;grid-template-columns:auto 1fr 1fr 1fr auto;gap:.5rem;align-items:center}
button{font:inherit;border:none;border-radius:6px;padding:.5rem .75rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:.35rem;transition:all .1s ease}
button:active{transform:scale(0.98)}
.btn-nav{background:#f1f5f9;color:var(--text);border:1px solid var(--border);padding:.5rem 1rem}
.btn-nav:hover{background:#e2e8f0}
.btn-accept{background:var(--green);color:#fff;font-size:15px;padding:.65rem 1rem}
.btn-accept:hover{background:var(--green-hover)}
.btn-reject{background:var(--red);color:#fff;font-size:15px;padding:.65rem 1rem}
.btn-reject:hover{background:var(--red-hover)}
.btn-pending{background:var(--amber);color:#fff;font-size:15px;padding:.65rem 1rem}
.btn-pending:hover{background:var(--amber-hover)}
.shortcut-tag{background:rgba(255,255,255,.3);padding:1px 5px;border-radius:3px;font-size:11px;font-family:monospace}
.btn-nav .shortcut-tag{background:rgba(0,0,0,.08)}
.shortcuts-footer{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--muted);padding-top:.15rem}
#message{font-size:12px;font-weight:600;color:var(--primary);min-height:16px}
</style></head><body><div class="container">
<div id="login" class="card">
<h2 style="font-size:1.1rem;margin-bottom:.25rem">Dataset Foundry Review</h2>
<p style="font-size:12px;color:var(--muted);margin-bottom:.5rem">Nhập mã token bảo mật để bắt đầu thẩm định</p>
<input id="token" type="password" autocomplete="off" placeholder="Nhập Review token...">
<button id="open">Mở Review</button>
</div>
<main id="review" hidden class="card">
<header>
<div class="header-left">
<h1>Dataset Foundry Review</h1>
<span id="name" class="asset-title"></span>
</div>
<div class="header-right">
<span id="status-badge" class="badge badge-pending">PENDING</span>
<span class="stat-pill"><strong id="position">0 / 0</strong></span>
<span id="stats-summary" class="stat-pill">✓ 0 | ✗ 0 | ? 0</span>
</div>
</header>
<div class="filter-bar">
<span style="font-size:12px;color:var(--muted);font-weight:600;margin-right:4px">Bộ lọc:</span>
<button class="filter-btn active" data-filter="images">🖼️ Chỉ Frame ảnh</button>
<button class="filter-btn" data-filter="all">Tất cả</button>
<button class="filter-btn" data-filter="pending">Chưa duyệt</button>
<button class="filter-btn" data-filter="accepted">Đã duyệt</button>
<button class="filter-btn" data-filter="rejected">Đã loại</button>
</div>
<div id="asset-container"><div id="asset"></div></div>
<details id="meta-details" class="meta-box">
<summary class="meta-summary">ℹ️ Chi tiết kỹ thuật &amp; Metadata (Click để mở)</summary>
<pre id="meta"></pre>
</details>
<div class="dock">
<textarea id="note" maxlength="4000" placeholder="Ghi chú (tùy chọn) - vd: phone dưới hộc bàn, ngoái đầu..."></textarea>
<div class="actions-row">
<button id="prev" class="btn-nav" title="Ảnh trước (Phím ←)"><span>← Trước</span><span class="shortcut-tag">←</span></button>
<button data-decision="accepted" class="btn-accept" title="Chấp nhận (Phím A)"><span>✓ Chấp nhận</span><span class="shortcut-tag">A</span></button>
<button data-decision="rejected" class="btn-reject" title="Loại bỏ (Phím R)"><span>✗ Loại bỏ</span><span class="shortcut-tag">R</span></button>
<button data-decision="pending" class="btn-pending" title="Tạm hoãn (Phím P)"><span>? Tạm hoãn</span><span class="shortcut-tag">P</span></button>
<button id="next" class="btn-nav" title="Ảnh tiếp (Phím →)"><span>Tiếp →</span><span class="shortcut-tag">→</span></button>
</div>
<div class="shortcuts-footer">
<span id="message"></span>
<span>💡 Phím tắt nhanh: <strong>A</strong> Chấp nhận &amp; chuyển tiếp | <strong>R</strong> Loại bỏ | <strong>P</strong> Tạm hoãn | <strong>←/→</strong> Điều hướng</span>
</div>
</div>
</main>
</div>
<script nonce="__NONCE__">
'use strict';
let token='',allItems=[],items=[],index=0,objectUrl='',activeFilter='images';
const $=id=>document.getElementById(id);
async function api(path,options={}){
options.headers={...(options.headers||{}),Authorization:'Bearer '+token};
const r=await fetch(path,options);
if(!r.ok)throw new Error((await r.json().catch(()=>({error:r.statusText}))).error);
return r;
}
function applyFilter(filterName){
activeFilter=filterName;
document.querySelectorAll('.filter-btn').forEach(b=>{
b.classList.toggle('active',b.dataset.filter===filterName);
});
if(filterName==='images'){
items=allItems.filter(x=>x.media_kind==='image');
if(!items.length)items=allItems.slice();
}else if(filterName==='pending'){
items=allItems.filter(x=>!x.decision||x.decision.decision==='pending');
}else if(filterName==='accepted'){
items=allItems.filter(x=>x.decision&&x.decision.decision==='accepted');
}else if(filterName==='rejected'){
items=allItems.filter(x=>x.decision&&x.decision.decision==='rejected');
}else{
items=allItems.slice();
}
index=0;
render();
}
async function openReview(){
token=$('token').value.trim();
if(!token){$('message').textContent='Vui lòng nhập token.';return}
try{localStorage.setItem('review_token',token)}catch(_){}
try{
allItems=await (await api('/api/candidates')).json();
$('login').style.display='none';
$('login').hidden=true;
$('review').hidden=false;
if(!allItems.some(x=>x.media_kind==='image')){
activeFilter='all';
}
applyFilter(activeFilter);
}catch(e){$('message').textContent=e.message}
}
function updateStats(){
let acc=0,rej=0,pen=0;
for(const it of allItems){
const d=(it.decision&&it.decision.decision)||'pending';
if(d==='accepted')acc++;
else if(d==='rejected')rej++;
else pen++;
}
$('stats-summary').textContent=`✓ ${acc} | ✗ ${rej} | ? ${pen}`;
}
async function render(){
if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl=''}
$('asset').replaceChildren();
updateStats();
if(!items.length){
$('position').textContent='0 / 0';
$('name').textContent='Không có item phù hợp bộ lọc';
$('meta').textContent='';
$('status-badge').textContent='EMPTY';
$('status-badge').className='badge badge-pending';
return;
}
if(index>=items.length)index=0;
const x=items[index];
$('position').textContent=`${index+1} / ${items.length}`;
$('name').textContent=x.original_name||x.asset_id;
$('meta').textContent=JSON.stringify(x,null,2);
const dec=(x.decision&&x.decision.decision)||'pending';
$('note').value=(x.decision&&x.decision.note)||'';
const badge=$('status-badge');
if(badge){
badge.className='badge badge-'+dec;
badge.textContent=dec.toUpperCase();
}
try{
const blob=await (await api('/api/assets/'+encodeURIComponent(x.asset_id))).blob();
objectUrl=URL.createObjectURL(blob);
let el;
if((x.media_kind||'')==='image')el=new Image();
else if(['video','audio'].includes(x.media_kind)){el=document.createElement(x.media_kind);el.controls=true}
else{el=document.createElement('a');el.textContent='Tải asset';el.download=x.original_name||'asset'}
el.src=objectUrl;
$('asset').append(el);
}catch(e){$('message').textContent=e.message}
}
async function decide(value,advance=true){
if(!items.length)return;
const x=items[index];
try{
x.decision=await (await api('/api/decisions/'+encodeURIComponent(x.asset_id),{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({decision:value,note:$('note').value})
})).json();
const foundInAll=allItems.find(a=>a.asset_id===x.asset_id);
if(foundInAll)foundInAll.decision=x.decision;
$('message').textContent=`Đã lưu: ${value.toUpperCase()} (#${index+1})`;
if(advance&&items.length>1){
index=(index+1)%items.length;
}
await render();
}catch(e){$('message').textContent=e.message}
}
try{
const savedToken=localStorage.getItem('review_token');
if(savedToken)$('token').value=savedToken;
}catch(_){}
$('open').onclick=openReview;
$('token').onkeydown=e=>{if(e.key==='Enter')openReview()};
$('prev').onclick=()=>{if(items.length){index=(index-1+items.length)%items.length;render()}};
$('next').onclick=()=>{if(items.length){index=(index+1)%items.length;render()}};
document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=()=>decide(b.dataset.decision,true));
document.querySelectorAll('.filter-btn').forEach(b=>b.onclick=()=>applyFilter(b.dataset.filter));
window.addEventListener('keydown',e=>{
if($('review').hidden)return;
if(document.activeElement===$('note')||document.activeElement===$('token'))return;
const k=e.key.toLowerCase();
if(k==='a'){e.preventDefault();decide('accepted',true)}
else if(k==='r'){e.preventDefault();decide('rejected',true)}
else if(k==='p'){e.preventDefault();decide('pending',true)}
else if(e.key==='ArrowLeft'){e.preventDefault();if(items.length){index=(index-1+items.length)%items.length;render()}}
else if(e.key==='ArrowRight'){e.preventDefault();if(items.length){index=(index+1)%items.length;render()}}
});
</script></body></html>"""
