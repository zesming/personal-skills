#!/usr/bin/env python3
"""Offline tests for provision_release.py.

The tests generate only temporary synthetic keys/certificates and use a fake
transport.  No project signing material, credentials, or AGC endpoint is
used.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agc_upload  # noqa: E402
import provision_release as provision  # noqa: E402


APP_ID = "1234567890123456789"  # Synthetic 19-digit fixture, not a production app.
BUNDLE = "com.example.termii"
CERT_ID = "cert-123"
PROFILE_ID = "profile-123"
CERT_URL = "https://obs.cn-north-4.myhuaweicloud.com/cert-123.cer"
PROFILE_URL = "https://obs.cn-north-4.myhuaweicloud.com/profile-123.p7b"


@unittest.skipUnless(provision.CRYPTO_AVAILABLE, "cryptography is required for signing tests")
class ProvisionReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "signing"
        self.project = Path(self.tempdir.name) / "harmonyos"
        self.project.mkdir()
        (self.project / "build-profile.json5").write_text(
            json.dumps(
                {
                    "app": {
                        "signingConfigs": [
                            {
                                "name": "default",
                                "type": "HarmonyOS",
                                "material": {
                                    "certpath": "debug.cer",
                                    "keyAlias": "debug",
                                    "keyPassword": "debug-password",
                                    "profile": "debug.p7b",
                                    "storeFile": "debug.p12",
                                    "storePassword": "debug-password",
                                },
                            }
                        ],
                        "products": [{"name": "default", "signingConfig": "default", "compileSdkVersion": 12}],
                    }
                }
            ),
            encoding="utf-8",
        )
        self.credentials = Path(self.tempdir.name) / "credentials.json"
        self.credentials.write_text(
            json.dumps({"client_id": "client-id", "client_secret": "secret-not-output"}), encoding="utf-8"
        )
        os.chmod(self.credentials, 0o600)
        self.ca_key = provision.ec.generate_private_key(provision.ec.SECP384R1())
        self.ca_cert = self._certificate(self.ca_key, self.ca_key.public_key(), "Test CA", "Test CA")
        self.transport = FakeTransport(self)
        self.password_encrypt_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _certificate(self, signing_key, public_key, subject_name, issuer_name):
        name = provision.x509.Name([provision.x509.NameAttribute(provision.NameOID.COMMON_NAME, subject_name)])
        issuer = provision.x509.Name([provision.x509.NameAttribute(provision.NameOID.COMMON_NAME, issuer_name)])
        now = provision.dt.datetime.now(provision.dt.timezone.utc)
        return (
            provision.x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer)
            .public_key(public_key)
            .serial_number(provision.x509.random_serial_number())
            .not_valid_before(now - provision.dt.timedelta(minutes=1))
            .not_valid_after(now + provision.dt.timedelta(days=365))
            .sign(signing_key, provision.hashes.SHA384())
        )

    def _profile_payload(self, certificate, *, profile_type="release", bundle=BUNDLE, app_gallery=True):
        certificate_bytes = (
            certificate
            if isinstance(certificate, bytes)
            else certificate.public_bytes(provision.serialization.Encoding.DER)
        )
        return plistlib.dumps(
            {
                "type": profile_type,
                "bundle-info": {
                    "bundle-name": bundle,
                    "distribution-certificate": [certificate_bytes],
                },
                "app-distribution-type": "app_gallery" if app_gallery else "enterprise",
                "validity": {
                    "not-before": "2020-01-01T00:00:00Z",
                    "not-after": "2099-01-01T00:00:00Z",
                },
            }
        )

    def _encrypt_password(self, signing_directory, password):
        self.password_encrypt_calls.append((Path(signing_directory), password))
        return "sdk-encrypted-password"

    def test_prepare_generates_encrypted_secp384r1_material_and_reuses_it(self):
        result = provision.provision_release(
            project=self.project,
            app_id=APP_ID,
            bundle_name=BUNDLE,
            signing_root=self.root,
            stage="prepare",
            create_signing=True,
            reuse_only=False,
        )
        directory = Path(result["localDirectory"])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual((directory / "key-password").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "release-key.pem").stat().st_mode & 0o777, 0o600)
        password = (directory / "key-password").read_bytes().strip()
        key = provision.serialization.load_pem_private_key((directory / "release-key.pem").read_bytes(), password=password)
        self.assertIsInstance(key.curve, provision.ec.SECP384R1)
        original_key = (directory / "release-key.pem").read_bytes()
        reused = provision.provision_release(
            project=self.project,
            app_id=APP_ID,
            bundle_name=BUNDLE,
            signing_root=self.root,
            stage="prepare",
            reuse_only=True,
        )
        self.assertEqual(reused["status"], "prepared")
        self.assertEqual((directory / "release-key.pem").read_bytes(), original_key)

    def test_root_first_certificate_bundle_is_reordered_from_csr_leaf(self):
        paths = provision._new_directory(self.root, BUNDLE)
        provision._generate_material(paths, BUNDLE)
        csr = provision.x509.load_pem_x509_csr(paths["csr"].read_bytes())

        root_key = provision.ec.generate_private_key(provision.ec.SECP384R1())
        root = self._certificate(root_key, root_key.public_key(), "Root CA", "Root CA")
        intermediate_key = provision.ec.generate_private_key(provision.ec.SECP384R1())
        intermediate = self._certificate(root_key, intermediate_key.public_key(), "Intermediate CA", "Root CA")
        leaf = self._certificate(intermediate_key, csr.public_key(), "Distribution", "Intermediate CA")

        # AGC returned this real-world ordering in the failing run.  The
        # local CSR must identify the leaf before issuer links are traversed.
        root_first = b"".join(
            certificate.public_bytes(provision.serialization.Encoding.PEM)
            for certificate in (root, intermediate, leaf)
        )
        fingerprint, ordered_pem, ordered = provision._verify_certificate_chain(root_first, paths)

        self.assertEqual(fingerprint, provision._fingerprint(leaf))
        self.assertEqual(
            [certificate.subject.rfc4514_string() for certificate in ordered],
            ["CN=Distribution", "CN=Intermediate CA", "CN=Root CA"],
        )
        self.assertEqual(
            provision._parse_certificates(ordered_pem),
            tuple(ordered),
        )

    def test_full_mocked_flow_posts_exact_release_payloads_and_configures_project(self):
        with mock.patch.object(provision, "verify_profile", return_value=None):
            result = provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                product="default",
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=self.transport,
                password_encryptor=self._encrypt_password,
            )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.transport.cert_posts, 1)
        self.assertEqual(self.transport.profile_posts, 1)
        self.assertEqual(self.transport.cert_body["certType"], 2)
        self.assertEqual(self.transport.profile_body, {
            "provisionName": f"{BUNDLE}-release-profile",
            "provisionType": 2,
            "certId": CERT_ID,
            "appId": APP_ID,
        })
        self.assertEqual(self.transport.download_headers, [{}, {}])
        config = json.loads((self.project / "build-profile.json5").read_text(encoding="utf-8"))
        configs = config["app"]["signingConfigs"]
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0]["name"], "default")
        release = next(item for item in configs if item["name"] == "agc-release")
        self.assertEqual(release["material"]["keyAlias"], "agc-release")
        self.assertEqual(release["material"]["keyPassword"], "sdk-encrypted-password")
        self.assertEqual(release["material"]["storePassword"], "sdk-encrypted-password")
        self.assertNotEqual(release["material"]["keyPassword"], (Path(result["localDirectory"]) / "key-password").read_text().strip())
        self.assertEqual(self.password_encrypt_calls[0][0], Path(result["localDirectory"]))
        self.assertEqual(config["app"]["products"][0]["signingConfig"], "agc-release")
        state = json.loads((Path(result["localDirectory"]) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["certPhase"], "verified")
        self.assertEqual(state["profilePhase"], "verified")
        self.assertEqual((Path(result["localDirectory"]) / "release.p12").stat().st_mode & 0o777, 0o600)

    def test_completed_state_reuses_without_duplicate_create_requests(self):
        with mock.patch.object(provision, "verify_profile", return_value=None):
            first = provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=self.transport,
                password_encryptor=self._encrypt_password,
            )
        second_transport = FakeTransport(self, fail_on_post=True)
        with mock.patch.object(provision, "verify_profile", return_value=None):
            second = provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                reuse_only=True,
                transport=second_transport,
                password_encryptor=self._encrypt_password,
            )
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["publicFingerprint"], first["publicFingerprint"])
        self.assertEqual(second_transport.cert_posts, 0)
        self.assertEqual(second_transport.profile_posts, 0)

    def test_uncertain_certificate_request_persists_stop_state_without_retry(self):
        failing = FakeTransport(self, fail_cert=True)
        with self.assertRaises(provision.ProvisionUncertain):
            provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=failing,
            )
        state_path = self.root / BUNDLE / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["certPhase"], "uncertain")
        with self.assertRaises(provision.ProvisionUncertain):
            provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=FakeTransport(self),
            )
        self.assertEqual(failing.cert_posts, 1)

    def test_reuse_only_without_state_does_not_generate_material(self):
        with self.assertRaises(provision.ProvisionError):
            provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                signing_root=self.root,
                reuse_only=True,
            )
        self.assertFalse(self.root.exists())

    def test_profile_verification_checks_canonical_fields_type_bundle_and_certificate(self):
        private = provision.ec.generate_private_key(provision.ec.SECP384R1())
        certificate = self._certificate(private, private.public_key(), "Distribution", "Test CA")
        payload = self._profile_payload(certificate)
        profile_path = Path(self.tempdir.name) / "profile.p7b"
        profile_path.write_bytes(b"cms")
        runner = lambda _profile, _output: payload
        fingerprint = provision._fingerprint(certificate)
        provision.verify_profile(
            profile_path,
            bundle_name=BUNDLE,
            app_id=APP_ID,
            certificate_fingerprint=fingerprint,
            app_gallery=True,
            openssl_runner=runner,
        )
        for bad_payload in (
            self._profile_payload(certificate, profile_type="debug"),
            self._profile_payload(certificate, bundle="com.other.app"),
            self._profile_payload(private.public_key().public_bytes(provision.serialization.Encoding.DER, provision.serialization.PublicFormat.SubjectPublicKeyInfo)),
            plistlib.dumps(
                {
                    "type": "debug",
                    "app-distribution-type": "enterprise",
                    "validity": self._profile_payload(certificate),
                    "metadata": {
                        "type": "release",
                        "bundle-info": {
                            "bundle-name": BUNDLE,
                            "distribution-certificate": [certificate.public_bytes(provision.serialization.Encoding.DER)],
                        },
                        "app-distribution-type": "app_gallery",
                    },
                }
            ),
        ):
            with self.assertRaises(provision.ProvisionProtocolError):
                provision.verify_profile(
                    profile_path,
                    bundle_name=BUNDLE,
                    app_id=APP_ID,
                    certificate_fingerprint=fingerprint,
                    app_gallery=True,
                    openssl_runner=lambda _profile, _output, data=bad_payload: data,
                )

    def test_empty_signing_configs_are_rejected_before_any_remote_request(self):
        profile = json.loads((self.project / "build-profile.json5").read_text(encoding="utf-8"))
        profile["app"]["signingConfigs"] = []
        (self.project / "build-profile.json5").write_text(json.dumps(profile), encoding="utf-8")
        with self.assertRaises(provision.ProvisionError):
            provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=self.transport,
            )
        self.assertEqual(self.transport.cert_posts, 0)
        self.assertFalse((self.root / BUNDLE).exists())

    def test_existing_agc_release_is_preserved_and_rejected_before_remote_request(self):
        profile = json.loads((self.project / "build-profile.json5").read_text(encoding="utf-8"))
        profile["app"]["signingConfigs"].append(
            {
                "name": "agc-release",
                "type": "HarmonyOS",
                "material": {"certpath": "other.cer", "keyAlias": "other"},
            }
        )
        (self.project / "build-profile.json5").write_text(json.dumps(profile), encoding="utf-8")
        with self.assertRaises(provision.ProvisionError):
            provision.provision_release(
                project=self.project,
                app_id=APP_ID,
                bundle_name=BUNDLE,
                credentials_path=self.credentials,
                signing_root=self.root,
                create_signing=True,
                reuse_only=False,
                transport=self.transport,
            )
        self.assertEqual(self.transport.cert_posts, 0)
        preserved = json.loads((self.project / "build-profile.json5").read_text(encoding="utf-8"))
        self.assertEqual(preserved["app"]["signingConfigs"][-1]["material"]["keyAlias"], "other")

    def test_cli_output_does_not_print_generated_password(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = provision.main(
                [
                    "--project",
                    str(self.project),
                    "--app-id",
                    APP_ID,
                    "--bundle-name",
                    BUNDLE,
                    "--signing-root",
                    str(self.root),
                    "--stage",
                    "prepare",
                    "--create-signing",
                ]
            )
        self.assertEqual(code, 0)
        password = (self.root / BUNDLE / "key-password").read_text(encoding="utf-8").strip()
        self.assertNotIn(password, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "prepared")


class FakeTransport:
    def __init__(self, case: ProvisionReleaseTests, *, fail_cert=False, fail_on_post=False):
        self.case = case
        self.fail_cert = fail_cert
        self.fail_on_post = fail_on_post
        self.cert_posts = 0
        self.profile_posts = 0
        self.cert_body = None
        self.profile_body = None
        self.download_headers = []
        self.certificate_body = None

    def request(self, method, url, headers=None, *, body=None, stream=None):
        if url.endswith("/api/oauth2/v1/token"):
            return agc_upload.HttpResponse(200, {}, b'{"access_token":"token-not-output"}')
        if method == "POST" and url.endswith(provision.API_CERT_PATH):
            self.cert_posts += 1
            if self.fail_cert:
                raise agc_upload.NetworkError("simulated connection loss")
            if self.fail_on_post:
                raise AssertionError("completed state attempted duplicate certificate request")
            self.cert_body = json.loads(body.decode("utf-8"))
            csr = provision.x509.load_pem_x509_csr(self.cert_body["csr"].encode("ascii"))
            cert_key = self.case.ca_key
            certificate = self.case._certificate(cert_key, csr.public_key(), "Distribution", "Test CA")
            self.certificate_body = certificate.public_bytes(provision.serialization.Encoding.PEM)
            return agc_upload.HttpResponse(
                200,
                {},
                json.dumps({"ret": {"code": 0}, "certInfo": {"id": CERT_ID, "certDownloadUrl": CERT_URL}}).encode(),
            )
        if method == "GET" and url == CERT_URL:
            self.download_headers.append(dict(headers or {}))
            return agc_upload.HttpResponse(200, {}, self.certificate_body)
        if method == "POST" and url.endswith(provision.API_PROFILE_PATH):
            self.profile_posts += 1
            if self.fail_on_post:
                raise AssertionError("completed state attempted duplicate Profile request")
            self.profile_body = json.loads(body.decode("utf-8"))
            return agc_upload.HttpResponse(
                200,
                {},
                json.dumps({"ret": {"code": 0}, "provisionInfo": {"id": PROFILE_ID, "provisionDownloadUrl": PROFILE_URL}}).encode(),
            )
        if method == "GET" and url == PROFILE_URL:
            self.download_headers.append(dict(headers or {}))
            return agc_upload.HttpResponse(200, {}, b"profile-cms")
        raise AssertionError(f"unexpected fake request {method} {url}")


if __name__ == "__main__":
    unittest.main()
