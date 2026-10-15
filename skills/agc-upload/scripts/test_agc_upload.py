#!/usr/bin/env python3
"""Offline tests for agc_upload.py.

Every network-facing test uses FakeTransport.  No test can reach AGC, and the
temporary package and credential files are removed by unittest's tempdir.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agc_upload as agc  # noqa: E402


APP_ID = "app-123"
BUNDLE = "com.example.termii"
OBJECT_ID = "CN/2026091001/object.app"
PACKAGE_ID = "pkg-123"
TEST_PACKAGE_ID = "test-pkg-123"
UPLOAD_URL = "https://obs.cn-north-4.myhuaweicloud.com/signed-object"
UPLOAD_HEADERS = {"Content-Type": "application/octet-stream", "x-test-signature": "signed"}


class FakeTransport:
    """Minimal transport with an explicit response queue and call capture."""

    def __init__(self, *, statuses=None, upload_response=200, register_error=None):
        self.statuses = list(statuses or [0])
        self.upload_response = upload_response
        self.register_error = register_error
        self.calls = []
        self.upload_body = None
        self.package_id = PACKAGE_ID

    def request(self, method, url, headers=None, *, body=None, stream=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
                "stream": stream,
            }
        )
        if url.endswith("/api/oauth2/v1/token"):
            return agc.HttpResponse(200, {}, b'{"access_token":"TOKEN-SECRET","expires_in":3600}')
        if method == "GET" and "/api/publish/v2/upload-url/for-obs" in url:
            body = {
                "ret": {"code": 0, "msg": "success"},
                "urlInfo": {
                    "objectId": OBJECT_ID,
                    "url": UPLOAD_URL,
                    "method": "PUT",
                    "headers": UPLOAD_HEADERS,
                },
            }
            return agc.HttpResponse(200, {}, json.dumps(body).encode())
        if method == "PUT" and url == UPLOAD_URL:
            self.upload_body = stream.read() if stream is not None else body
            return agc.HttpResponse(self.upload_response, {}, b"")
        if method == "PUT" and "/api/publish/v3/app-package-info" in url:
            if self.register_error is not None:
                raise self.register_error
            body = {"ret": {"code": 0, "msg": "success"}, "packageId": PACKAGE_ID}
            return agc.HttpResponse(200, {}, json.dumps(body).encode())
        if method == "POST" and "/api/publish/v2/test/version/pkg" in url:
            self.package_id = TEST_PACKAGE_ID
            body = {"ret": {"code": 0, "msg": "success"}, "pkgVersion": [TEST_PACKAGE_ID]}
            return agc.HttpResponse(200, {}, json.dumps(body).encode())
        if method == "GET" and "/api/publish/v3/package/compile/status" in url:
            status = self.statuses.pop(0) if self.statuses else 0
            body = {
                "ret": {"code": 0, "msg": "success"},
                "pkgStateList": [{"pkgId": self.package_id, "successStatus": status}],
            }
            return agc.HttpResponse(200, {}, json.dumps(body).encode())
        if method == "GET" and "/api/publish/v2/appid-list" in url:
            body = {"ret": {"code": 0}, "appids": [{"key": "Termii", "value": APP_ID}]}
            return agc.HttpResponse(200, {}, json.dumps(body).encode())
        raise AssertionError(f"unexpected fake request: {method} {url}")


class AgcUploadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.package = self.root / "release.app"
        self._make_package()
        self.credentials = self.root / "credentials.json"
        self.credentials.write_text(
            json.dumps({"client_id": "client-1", "client_secret": "SECRET-DO-NOT-PRINT"}),
            encoding="utf-8",
        )
        os.chmod(self.credentials, 0o600)

    def tearDown(self):
        self.tempdir.cleanup()

    def _make_package(self, *, debug=False, bundle=BUNDLE, module_debug=False):
        pack_info = {
            "summary": {
                "app": {
                    "bundleName": bundle,
                    "version": {"name": "1.0.0", "code": 7, "buildVersion": 42},
                }
            },
            "buildVersion": 42,
            "buildRelease": not debug,
            "metadataRelease": "release-metadata",
        }
        module_buffer = io.BytesIO()
        with zipfile.ZipFile(module_buffer, "w") as module_zip:
            module_zip.writestr(
                "module.json",
                json.dumps(
                    {
                        "module": {"name": "entry", "type": "entry"},
                        "permissions": [{"name": "ohos.permission.INTERNET"}],
                        "debug": module_debug,
                    }
                ),
            )
        with zipfile.ZipFile(self.package, "w") as package_zip:
            package_zip.writestr("pack.info", json.dumps(pack_info))
            package_zip.writestr("entry.hap", module_buffer.getvalue())

    def _assert_json_output_safe(self, value):
        serialized = json.dumps(value)
        self.assertNotIn("TOKEN-SECRET", serialized)
        self.assertNotIn("SECRET-DO-NOT-PRINT", serialized)
        self.assertNotIn("myhuaweicloud", serialized)
        self.assertNotIn("headers", value)

    def test_inspect_is_offline_and_reads_hash_pack_info_and_module(self):
        info = agc.inspect_package(self.package)
        self.assertEqual(info.bundle_name, BUNDLE)
        self.assertEqual(info.version_name, "1.0.0")
        self.assertEqual(info.build_version, 42)
        self.assertEqual(info.metadata_release, "release-metadata")
        self.assertEqual(len(info.modules), 1)
        self.assertEqual(info.modules[0]["moduleName"], "entry")
        self.assertEqual(info.sha256, hashlib.sha256(self.package.read_bytes()).hexdigest())
        result = agc.package_info_json(info)
        self.assertFalse(result["signatureVerification"]["cryptographicallyVerified"])

    def test_nested_module_name_does_not_use_permission_name(self):
        info = agc.inspect_package(self.package)
        self.assertEqual(info.modules[0]["moduleName"], "entry")
        self.assertNotEqual(info.modules[0]["moduleName"], "ohos.permission.INTERNET")

    def test_dry_run_needs_no_credentials_or_transport(self):
        result = agc.upload_package(
            self.package,
            app_id=APP_ID,
            bundle_name=BUNDLE,
            dry_run=True,
        )
        self.assertEqual(result["action"], "dry-run")
        self.assertEqual(result["sha256"], hashlib.sha256(self.package.read_bytes()).hexdigest())

    def test_upload_sequence_streams_binary_and_keeps_api_auth_off_raw_put(self):
        transport = FakeTransport()
        receipt = self.root / "receipt.json"
        result = agc.upload_package(
            self.package,
            app_id=APP_ID,
            bundle_name=BUNDLE,
            channel="testing",
            credentials_path=self.credentials,
            receipt_path=receipt,
            transport=transport,
            poll_timeout=1,
            poll_interval=0,
        )
        self.assertEqual(result["objectId"], OBJECT_ID)
        self.assertEqual(result["packageId"], TEST_PACKAGE_ID)
        self.assertEqual(result["compileState"], "ready")
        self.assertEqual(transport.upload_body, self.package.read_bytes())
        put = next(call for call in transport.calls if call["method"] == "PUT" and call["url"] == UPLOAD_URL)
        self.assertNotIn("Authorization", put["headers"])
        self.assertEqual(put["headers"]["Content-Type"], UPLOAD_HEADERS["Content-Type"])
        self.assertEqual(put["headers"]["x-test-signature"], "signed")
        self.assertFalse(any("app-package-info" in call["url"] for call in transport.calls))
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "complete")
        self.assertEqual(saved["objectId"], OBJECT_ID)
        self.assertEqual(saved["packageId"], TEST_PACKAGE_ID)
        self._assert_json_output_safe(saved)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_existing_receipt_is_never_overwritten_by_upload(self):
        receipt = self.root / "receipt.json"
        receipt.write_text("existing", encoding="utf-8")
        os.chmod(receipt, 0o600)
        with self.assertRaises(agc.ReceiptError):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                receipt_path=receipt,
                transport=FakeTransport(),
            )
        self.assertEqual(receipt.read_text(encoding="utf-8"), "existing")

    def test_pending_timeout_is_explicit_and_resume_polls_without_reupload_or_reregister(self):
        receipt = self.root / "receipt.json"
        initial = FakeTransport(statuses=[1])
        with self.assertRaises(agc.PollTimeout):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                receipt_path=receipt,
                transport=initial,
                poll_timeout=0,
                poll_interval=0,
            )
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(saved["compileState"], "pending")
        self.assertEqual(saved["registrationState"], "registered")
        resumed = FakeTransport(statuses=[0])
        result = agc.resume_receipt(
            receipt,
            credentials_path=self.credentials,
            transport=resumed,
            poll_timeout=1,
            poll_interval=0,
        )
        self.assertEqual(result["compileState"], "ready")
        self.assertFalse(any("upload-url" in call["url"] for call in resumed.calls))
        self.assertFalse(any("app-package-info" in call["url"] for call in resumed.calls))

    def test_uncertain_registration_stops_resume_before_rebinding(self):
        receipt = self.root / "receipt.json"
        initial = FakeTransport(register_error=agc.NetworkError("simulated network loss"))
        with self.assertRaises(agc.NetworkError):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                receipt_path=receipt,
                transport=initial,
                poll_timeout=1,
                poll_interval=0,
            )
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(saved["registrationState"], "uncertain")
        resumed = FakeTransport()
        with self.assertRaises(agc.UnsafeResumeError):
            agc.resume_receipt(receipt, credentials_path=self.credentials, transport=resumed)
        self.assertFalse(any("app-package-info" in call["url"] for call in resumed.calls))

    def test_cancelled_registration_is_uncertain_and_cannot_be_retried(self):
        receipt = self.root / "cancelled-receipt.json"
        initial = FakeTransport(register_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                receipt_path=receipt,
                transport=initial,
                poll_timeout=1,
                poll_interval=0,
            )
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(saved["registrationState"], "uncertain")
        with self.assertRaises(agc.UnsafeResumeError):
            agc.resume_receipt(receipt, credentials_path=self.credentials, transport=FakeTransport())

    def test_receipt_lock_is_non_reentrant_across_store_instances(self):
        receipt = self.root / "locked-receipt.json"
        first = agc.ReceiptStore(receipt)
        first.acquire()
        try:
            second = agc.ReceiptStore(receipt)
            with self.assertRaises(agc.ReceiptError):
                second.acquire()
        finally:
            first.release()

    def test_malformed_upload_url_response_fails_without_raw_put(self):
        class Malformed(FakeTransport):
            def request(self, method, url, headers=None, *, body=None, stream=None):
                if method == "GET" and "/api/publish/v2/upload-url/for-obs" in url:
                    self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body, "stream": stream})
                    return agc.HttpResponse(200, {}, b'{"ret":{"code":0}}')
                return super().request(method, url, headers, body=body, stream=stream)

        fake = Malformed()
        with self.assertRaises(agc.ProtocolError):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                receipt_path=self.root / "malformed-receipt.json",
                transport=fake,
            )
        self.assertFalse(any(call["method"] == "PUT" and call["url"] == UPLOAD_URL for call in fake.calls))

    def test_debug_or_test_candidate_is_rejected(self):
        self._make_package(debug=True)
        with self.assertRaises(agc.PackageError):
            agc.inspect_package(self.package)

    def test_nested_module_debug_candidate_is_rejected(self):
        self._make_package(module_debug=True)
        with self.assertRaises(agc.PackageError):
            agc.inspect_package(self.package)

    def test_bundle_mismatch_is_rejected_before_credentials_or_network(self):
        fake = FakeTransport()
        with self.assertRaises(agc.PackageError):
            agc.upload_package(
                self.package,
                app_id=APP_ID,
                bundle_name="com.other.app",
                credentials_path=self.credentials,
                transport=fake,
            )
        self.assertEqual(fake.calls, [])

    def test_discover_uses_package_type_7_and_returns_matching_identity(self):
        result = agc.discover_app_ids(BUNDLE, credentials_path=self.credentials, transport=FakeTransport())
        self.assertEqual(result, {"bundleName": BUNDLE, "appIds": [{"displayName": "Termii", "appId": APP_ID}]})

    def test_private_ip_or_arbitrary_upload_host_is_rejected(self):
        for url in ("http://obs.myhuaweicloud.com/object", "https://127.0.0.1/object", "https://upload.example/object"):
            with self.assertRaises(agc.ProtocolError):
                agc.validate_upload_url(url)

    def test_mainland_obs_cn_upload_url_is_accepted(self):
        url = "https://nsp-appgallery-agcfs-drcn.obs.cn-north-2.myhuaweicloud.cn/object"
        self.assertEqual(agc.validate_upload_url(url), url)

    @unittest.skipUnless(agc._CRYPTO_AVAILABLE, "cryptography is optional on the system Python")
    def test_service_account_jwt_has_ps256_and_verifiable_signature(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        jwt = agc.create_service_account_jwt(
            {"key_id": "key-1", "private_key": pem, "sub_account": "sub-1"}, now=1_700_000_000
        )
        encoded_header, encoded_payload, encoded_signature = jwt.split(".")
        header = json.loads(agc._b64url_decode(encoded_header))
        payload = json.loads(agc._b64url_decode(encoded_payload))
        self.assertEqual(header, {"alg": "PS256", "kid": "key-1", "typ": "JWT"})
        self.assertEqual(payload["aud"], agc.SERVICE_ACCOUNT_AUDIENCE)
        self.assertEqual(payload["iss"], "sub-1")
        self.assertEqual(payload["exp"] - payload["iat"], 3600)
        private.public_key().verify(
            agc._b64url_decode(encoded_signature),
            f"{encoded_header}.{encoded_payload}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )


if __name__ == "__main__":
    unittest.main()
