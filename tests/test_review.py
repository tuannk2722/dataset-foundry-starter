import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from dataset_foundry.review import ReviewStore, create_review_server


class ReviewUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        asset = self.run / "raw" / "source" / "asset.txt"
        asset.parent.mkdir(parents=True)
        self.content = b"candidate bytes"
        asset.write_bytes(self.content)
        self.record = {
            "asset_id": "a" * 20,
            "parent_id": None,
            "source_id": "source",
            "source_adapter": "local_dir",
            "origin": "synthetic",
            "original_name": "asset.txt",
            "license": "self-created",
            "rights_note": "synthetic test",
            "mime_guess": "text/plain",
            "media_kind": "text",
            "review_status": "unreviewed",
            "label": None,
            "split": None,
            "status": "downloaded",
            "file_path": "raw/source/asset.txt",
            "bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }
        skipped = {
            **self.record,
            "asset_id": "b" * 20,
            "status": "skipped",
            "file_path": None,
            "reason": "test skip",
        }
        self.manifest = self.run / "manifest.jsonl"
        self.manifest.write_text(
            json.dumps(self.record) + "\n" + json.dumps(skipped) + "\n",
            encoding="utf-8",
        )
        self.manifest_before = self.manifest.read_bytes()
        self.token = "test-review-token"
        self.server = create_review_server(self.run, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, path, *, token=None, method="GET", payload=None):
        data = None
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        return self.opener.open(request, timeout=5)

    def test_token_required_and_index_discloses_no_candidate_data(self):
        with self._request("/") as response:
            page = response.read().decode("utf-8")
        self.assertIn("Dataset Foundry Review", page)
        self.assertNotIn(self.record["asset_id"], page)
        with self.assertRaises(HTTPError) as caught:
            self._request("/api/candidates")
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            self._request("/api/candidates", token="wrong")
        self.assertEqual(caught.exception.code, 401)

    def test_decision_persists_in_sidecar_without_modifying_manifest(self):
        with self._request("/api/candidates", token=self.token) as response:
            candidates = json.load(response)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["decision"]["decision"], "pending")

        path = f"/api/decisions/{self.record['asset_id']}"
        with self._request(
            path,
            token=self.token,
            method="POST",
            payload={"decision": "accepted", "note": "looks usable"},
        ) as response:
            saved = json.load(response)
        self.assertEqual(saved["decision"], "accepted")
        self.assertEqual(self.manifest.read_bytes(), self.manifest_before)
        self.assertTrue((self.run / "review.sqlite3").is_file())

        reopened = ReviewStore(self.run)
        decision = reopened.candidates()[0]["decision"]
        self.assertEqual(decision["decision"], "accepted")
        self.assertEqual(decision["note"], "looks usable")

    def test_rejects_invalid_decision_and_unknown_asset(self):
        with self.assertRaises(HTTPError) as caught:
            self._request(
                f"/api/decisions/{self.record['asset_id']}",
                token=self.token,
                method="POST",
                payload={"decision": "labeled"},
            )
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self._request(
                "/api/decisions/not-whitelisted",
                token=self.token,
                method="POST",
                payload={"decision": "accepted"},
            )
        self.assertEqual(caught.exception.code, 404)

    def test_serves_only_whitelisted_checksum_verified_assets(self):
        with self._request(
            f"/api/assets/{self.record['asset_id']}", token=self.token
        ) as response:
            self.assertEqual(response.read(), self.content)

        with self.assertRaises(HTTPError) as caught:
            self._request("/api/assets/not-whitelisted", token=self.token)
        self.assertEqual(caught.exception.code, 404)

        (self.run / self.record["file_path"]).write_bytes(b"tampered content")
        with self.assertRaises(HTTPError) as caught:
            self._request(f"/api/assets/{self.record['asset_id']}", token=self.token)
        self.assertEqual(caught.exception.code, 409)

    def test_unsafe_manifest_path_is_never_served(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        unsafe = {
            **self.record,
            "file_path": "../outside.txt",
            "bytes": outside.stat().st_size,
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        }
        self.manifest.write_text(json.dumps(unsafe) + "\n", encoding="utf-8")
        # The server's whitelist is an immutable startup snapshot, so use a new store.
        store = ReviewStore(self.run, self.root / "unsafe.sqlite3")
        with self.assertRaisesRegex(ValueError, "escapes"):
            handle, _ = store.open_verified_asset(unsafe["asset_id"])
            handle.close()

    def test_non_loopback_binding_is_refused(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_review_server(self.run, self.token, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
