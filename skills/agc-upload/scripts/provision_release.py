#!/usr/bin/env python3
"""Prepare and provision a local HarmonyOS release signing identity.

This helper owns the local signing material and the two AppGallery Connect
provisioning calls.  It does not build, sign, upload, or publish an app.  The
normal mode is reuse-only; ``--create-signing`` is an explicit opt-in for the
first certificate/profile request.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - supported hosts are POSIX.
    fcntl = None  # type: ignore[assignment]

try:
    import agc_upload
except ImportError:  # pragma: no cover - direct execution adds this directory.
    from . import agc_upload  # type: ignore


try:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - stock Python can still show --help.
    x509 = hashes = serialization = ec = NameOID = pkcs12 = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False


API_CERT_PATH = "/api/publish/v3/cert"
API_PROFILE_PATH = "/api/publish/v3/provision"
STATE_SCHEMA = 1
CERT_TYPE_RELEASE = 2
PROFILE_TYPE_RELEASE = 2
DEFAULT_SIGNING_ROOT = Path.home() / ".config" / "agc-upload" / "signing"
DEFAULT_PASSWORD_FILE_NAME = "key-password"
ENCRYPTOR_FILE_NAME = "encrypt_signing_config.cjs"
KEY_FILE_NAME = "release-key.pem"
CSR_FILE_NAME = "release.csr"
CERT_FILE_NAME = "release-chain.cer"
PKCS12_FILE_NAME = "release.p12"
PROFILE_FILE_NAME = "release.p7b"
STATE_FILE_NAME = "state.json"


class ProvisionError(Exception):
    """Expected, safe-to-report provisioning failure."""


class ProvisionUncertain(ProvisionError):
    """An external create request may have succeeded; never repeat it."""


class ProvisionProtocolError(ProvisionError):
    pass


PasswordEncryptor = Callable[[Path, str], str]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_identifier(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value or "://" in value:
        raise ProvisionProtocolError(f"invalid {description}")
    if len(value) > 1024:
        raise ProvisionProtocolError(f"oversized {description}")
    return value


def _safe_bundle_name(bundle_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", bundle_name):
        raise ProvisionError("bundle-name contains unsupported path characters")
    return bundle_name


def _require_mode(path: Path, mode: int = 0o600) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError(f"required local file is unavailable: {path.name}") from exc
    if not info or not (info.st_mode & 0o100000) or (info.st_mode & 0o777) != mode:
        raise ProvisionError(f"local file {path.name} must have mode {mode:04o}")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        temp.unlink(missing_ok=True)


def _read_password(path: Path) -> bytes:
    _require_mode(path)
    try:
        value = path.read_bytes().strip()
    except OSError as exc:
        raise ProvisionError("private key password file cannot be read") from exc
    if not value or b"\x00" in value or b"\n" in value or b"\r" in value:
        raise ProvisionError("private key password file is invalid")
    return value


def _password_file(path: Path, *, create: bool) -> Tuple[Path, bytes]:
    if path.exists():
        return path, _read_password(path)
    if not create:
        raise ProvisionError("private key password file is missing")
    value = secrets.token_urlsafe(32).encode("ascii")
    _atomic_write(path, value + b"\n", 0o600)
    return path, value


def _require_crypto() -> None:
    if not CRYPTO_AVAILABLE:
        raise ProvisionError("cryptography is required for release signing provisioning")


def _public_key_bytes(key: Any) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _fingerprint(certificate: Any) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex()


def _state_path(directory: Path) -> Path:
    return directory / STATE_FILE_NAME


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    # Only this script's fixed state schema is persisted; URLs are retained
    # solely for crash recovery and are never printed.
    _atomic_write(path, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def _read_state(path: Path) -> Dict[str, Any]:
    _require_mode(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("signing state is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise ProvisionError("unsupported signing state")
    return value


def _paths(directory: Path) -> Dict[str, Path]:
    return {
        "directory": directory,
        "state": directory / STATE_FILE_NAME,
        "key": directory / KEY_FILE_NAME,
        "csr": directory / CSR_FILE_NAME,
        "certificate": directory / CERT_FILE_NAME,
        "pkcs12": directory / PKCS12_FILE_NAME,
        "profile": directory / PROFILE_FILE_NAME,
        "password": directory / DEFAULT_PASSWORD_FILE_NAME,
    }


def _new_directory(root: Path, bundle_name: str) -> Dict[str, Path]:
    directory = root / bundle_name
    if directory.exists():
        if not directory.is_dir():
            raise ProvisionError("signing path is not a directory")
        if any(directory.iterdir()):
            raise ProvisionError("signing directory exists without reusable state; refusing to rotate identity")
    else:
        directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    return _paths(directory)


def _existing_directory(root: Path, bundle_name: str) -> Dict[str, Path]:
    directory = root / bundle_name
    if not directory.is_dir():
        raise ProvisionError("reuse-only requested but no signing state exists")
    os.chmod(directory, 0o700)
    paths = _paths(directory)
    if not paths["state"].exists():
        raise ProvisionError("signing directory has no state; refusing to create a replacement identity")
    return paths


def _generate_material(
    paths: Mapping[str, Path], bundle_name: str, supplied_password: Optional[bytes] = None
) -> Dict[str, Any]:
    _require_crypto()
    if any(paths[name].exists() for name in ("key", "csr", "password", "certificate", "pkcs12", "profile")):
        raise ProvisionError("signing material already exists without reusable state")
    if supplied_password is None:
        _, password = _password_file(paths["password"], create=True)
    else:
        _atomic_write(paths["password"], supplied_password + b"\n", 0o600)
        password = supplied_password
    private_key = ec.generate_private_key(ec.SECP384R1())
    encrypted_key = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, bundle_name)]))
        .sign(private_key, hashes.SHA384())
    )
    _atomic_write(paths["key"], encrypted_key, 0o600)
    _atomic_write(paths["csr"], csr.public_bytes(serialization.Encoding.PEM), 0o600)
    return {
        "schema": STATE_SCHEMA,
        "bundleName": bundle_name,
        "keyAlgorithm": "EC-secp384r1",
        "keyAlias": "agc-release",
        "keyPath": str(paths["key"]),
        "csrPath": str(paths["csr"]),
        "passwordPath": str(paths["password"]),
        "certificatePath": str(paths["certificate"]),
        "pkcs12Path": str(paths["pkcs12"]),
        "profilePath": str(paths["profile"]),
        "certPhase": "not_requested",
        "profilePhase": "not_requested",
        "configured": False,
    }


def _validate_state(
    state: Mapping[str, Any], paths: Mapping[str, Path], bundle_name: str, app_id: str, product: str
) -> None:
    if state.get("bundleName") != bundle_name:
        raise ProvisionError("signing state bundleName does not match the requested bundle")
    if state.get("appId") not in (None, app_id):
        raise ProvisionError("signing state appId does not match the requested app")
    if state.get("product") not in (None, product):
        raise ProvisionError("signing state product does not match the requested product")
    if state.get("keyAlgorithm") != "EC-secp384r1":
        raise ProvisionError("signing state uses an unsupported key algorithm")
    if state.get("keyAlias") not in (None, "agc-release"):
        raise ProvisionError("signing state uses an unsupported key alias")
    config_name = state.get("configName")
    if config_name is not None:
        if not isinstance(config_name, str) or not config_name or "\n" in config_name or "\r" in config_name:
            raise ProvisionError("signing state has an invalid signing configuration name")
    for key in ("key", "csr", "password"):
        _require_mode(paths[key])


def _load_credentials(path: Optional[os.PathLike[str] | str]) -> Mapping[str, Any]:
    resolved = agc_upload.resolve_credentials_path(path)
    if resolved is None:
        raise ProvisionError("credentials are required for provisioning")
    try:
        return agc_upload.load_credentials(resolved)
    except agc_upload.AgcError as exc:
        raise ProvisionError(str(exc)) from exc


def _client(credentials: Mapping[str, Any], transport: Optional[Any]) -> agc_upload.AgcClient:
    return agc_upload.AgcClient(credentials, transport=transport)


def _parse_certificates(data: bytes) -> Tuple[Any, ...]:
    _require_crypto()
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----\s*.*?\s*-----END CERTIFICATE-----",
        data,
        flags=re.DOTALL,
    )
    certificates = []
    if blocks:
        for block in blocks:
            certificates.append(x509.load_pem_x509_certificate(block))
    else:
        try:
            certificates.append(x509.load_der_x509_certificate(data))
        except ValueError as exc:
            raise ProvisionProtocolError("certificate download is neither PEM nor DER") from exc
    if not certificates:
        raise ProvisionProtocolError("certificate download has no certificates")
    return tuple(certificates)


def _cert_time(certificate: Any, attribute: str) -> dt.datetime:
    modern = getattr(certificate, f"{attribute}_utc", None)
    if modern is not None:
        return modern
    return getattr(certificate, attribute).replace(tzinfo=dt.timezone.utc)


def _verify_certificate_chain(data: bytes, paths: Mapping[str, Path]) -> Tuple[str, bytes, Tuple[Any, ...]]:
    _require_crypto()
    chain = _parse_certificates(data)
    try:
        csr = x509.load_pem_x509_csr(paths["csr"].read_bytes())
        key = serialization.load_pem_private_key(paths["key"].read_bytes(), password=_read_password(paths["password"]))
    except (OSError, ValueError) as exc:
        raise ProvisionError("local key or CSR cannot be loaded") from exc
    matching = [cert for cert in chain if _public_key_bytes(csr.public_key()) == _public_key_bytes(cert.public_key())]
    if len(matching) != 1:
        raise ProvisionProtocolError("downloaded certificate public key does not match the CSR")
    leaf = matching[0]
    # AGC can return root-first PEM bundles. Identify the leaf by our CSR,
    # then order and verify issuer links before writing SDK/P12 material.
    ordered = [leaf]
    remaining = [cert for cert in chain if cert != leaf]
    while remaining:
        parents = [cert for cert in remaining if cert.subject == ordered[-1].issuer]
        if len(parents) != 1:
            raise ProvisionProtocolError("downloaded certificate chain has ambiguous or missing issuers")
        parent = parents[0]
        try:
            ordered[-1].verify_directly_issued_by(parent)
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise ProvisionProtocolError("downloaded certificate chain signature is invalid") from exc
        ordered.append(parent)
        remaining.remove(parent)
    chain = tuple(ordered)
    now = dt.datetime.now(dt.timezone.utc)
    if any(not (_cert_time(cert, "not_valid_before") <= now <= _cert_time(cert, "not_valid_after")) for cert in chain):
        raise ProvisionProtocolError("downloaded certificate is outside its validity period")
    if _public_key_bytes(key.public_key()) != _public_key_bytes(csr.public_key()):
        raise ProvisionError("local private key does not match the CSR")
    return _fingerprint(leaf), b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain), chain


def _generate_pkcs12(paths: Mapping[str, Path], chain: Sequence[Any], key_alias: str) -> None:
    _require_crypto()
    key = serialization.load_pem_private_key(paths["key"].read_bytes(), password=_read_password(paths["password"]))
    leaf = chain[0]
    additional = list(chain[1:])
    data = pkcs12.serialize_key_and_certificates(
        name=key_alias.encode("utf-8"),
        key=key,
        cert=leaf,
        cas=additional or None,
        encryption_algorithm=serialization.BestAvailableEncryption(_read_password(paths["password"])),
    )
    _atomic_write(paths["pkcs12"], data, 0o600)


def _validate_download_url(url: Any) -> str:
    try:
        return agc_upload.validate_upload_url(url)
    except agc_upload.AgcError as exc:
        raise ProvisionProtocolError(f"download URL rejected: {exc}") from exc


def _download(transport: Any, url: str) -> bytes:
    # Signed object downloads authenticate themselves.  Deliberately pass no
    # AGC Authorization/client_id headers and never expose the URL.
    response = transport.request("GET", url, {}, body=None)
    if response.status != 200:
        raise agc_upload.HttpResponseError(response.status)
    return bytes(response.body)


def _request_cert(
    client: agc_upload.AgcClient,
    state: Dict[str, Any],
    state_path: Path,
    *,
    cert_name: str,
    allow_create: bool,
) -> None:
    if state.get("certPhase") in {"requesting", "uncertain"}:
        raise ProvisionUncertain("certificate request outcome is uncertain; inspect AGC before continuing")
    if state.get("certPhase") == "not_requested":
        if not allow_create:
            raise ProvisionError("reuse-only requested; no new certificate request is allowed")
        state["certPhase"] = "requesting"
        _write_state(state_path, state)
        try:
            body = {"csr": Path(state["csrPath"]).read_text(encoding="ascii"), "certName": cert_name, "certType": CERT_TYPE_RELEASE}
            payload = client._api_request("POST", API_CERT_PATH, body=body, mutation=True)
            info = payload.get("certInfo")
            if not isinstance(info, Mapping):
                raise ProvisionProtocolError("certificate response is missing certInfo")
            cert_id = _safe_identifier(info.get("id"), "certificate id")
            download_url = _validate_download_url(info.get("certDownloadUrl"))
        except BaseException as exc:
            state["certPhase"] = "uncertain"
            state["lastError"] = type(exc).__name__
            _write_state(state_path, state)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionUncertain("certificate request outcome is uncertain") from exc
        state.update({"certId": cert_id, "certDownloadUrl": download_url, "certPhase": "download_pending", "lastError": None})
        _write_state(state_path, state)


def _finish_certificate(
    state: Dict[str, Any], paths: Mapping[str, Path], state_path: Path, transport: Any
) -> None:
    phase = state.get("certPhase")
    if phase == "verified":
        _require_mode(paths["certificate"])
        _require_mode(paths["pkcs12"])
        fingerprint, _, _ = _verify_certificate_chain(paths["certificate"].read_bytes(), paths)
        if fingerprint != state.get("certFingerprint"):
            raise ProvisionError("stored certificate fingerprint does not match signing state")
        return
    if phase != "download_pending":
        raise ProvisionUncertain("certificate is not in a recoverable download state")
    url = state.get("certDownloadUrl")
    if not isinstance(url, str):
        raise ProvisionUncertain("certificate download URL is unavailable; do not create another certificate")
    data = _download(transport, url)
    fingerprint, chain_pem, chain = _verify_certificate_chain(data, paths)
    _atomic_write(paths["certificate"], chain_pem, 0o600)
    _generate_pkcs12(paths, chain, str(state.get("keyAlias") or "agc-release"))
    state.update({"certPhase": "verified", "certFingerprint": fingerprint, "lastError": None})
    _write_state(state_path, state)


def _request_profile(
    client: agc_upload.AgcClient,
    state: Dict[str, Any],
    state_path: Path,
    *,
    provision_name: str,
    app_id: str,
    allow_create: bool,
) -> None:
    if state.get("profilePhase") in {"requesting", "uncertain"}:
        raise ProvisionUncertain("Profile request outcome is uncertain; inspect AGC before continuing")
    if state.get("profilePhase") == "not_requested":
        if not allow_create:
            raise ProvisionError("reuse-only requested; no new Profile request is allowed")
        if state.get("certPhase") != "verified" or not state.get("certId"):
            raise ProvisionError("verified certificate is required before requesting Profile")
        state["profilePhase"] = "requesting"
        _write_state(state_path, state)
        try:
            payload = client._api_request(
                "POST",
                API_PROFILE_PATH,
                body={
                    "provisionName": provision_name,
                    "provisionType": PROFILE_TYPE_RELEASE,
                    "certId": state["certId"],
                    "appId": app_id,
                },
                mutation=True,
            )
            info = payload.get("provisionInfo")
            if not isinstance(info, Mapping):
                raise ProvisionProtocolError("Profile response is missing provisionInfo")
            profile_id = _safe_identifier(info.get("id"), "Profile id")
            download_url = _validate_download_url(info.get("provisionDownloadUrl"))
        except BaseException as exc:
            state["profilePhase"] = "uncertain"
            state["lastError"] = type(exc).__name__
            _write_state(state_path, state)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionUncertain("Profile request outcome is uncertain") from exc
        state.update({"profileId": profile_id, "profileDownloadUrl": download_url, "profilePhase": "download_pending", "lastError": None})
        _write_state(state_path, state)


def _openssl_profile_payload(profile_path: Path, output_path: Path) -> bytes:
    formats = ("DER", "PEM")
    last_error = None
    for form in formats:
        try:
            result = subprocess.run(
                ["openssl", "cms", "-verify", "-noverify", "-inform", form, "-in", str(profile_path), "-out", str(output_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            continue
        if result.returncode == 0:
            try:
                return output_path.read_bytes()
            except OSError as exc:
                last_error = exc
    raise ProvisionProtocolError("Profile CMS signature verification failed") from last_error


def _parse_profile_payload(data: bytes) -> Any:
    try:
        return plistlib.loads(data)
    except Exception:
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvisionProtocolError("verified Profile payload is not a plist or JSON object") from exc
        if not isinstance(value, Mapping):
            raise ProvisionProtocolError("verified Profile payload is not an object")
        return value


def _profile_type_ok(payload: Any) -> bool:
    # HarmonyOS embeds this discriminator at the Profile root.  Looking for a
    # matching value in arbitrary nested objects would allow an unrelated
    # metadata field to make a debug Profile appear releasable.
    return isinstance(payload, Mapping) and payload.get("type") == "release"


def _profile_bundle_ok(payload: Any, bundle_name: str, app_id: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    bundle_info = payload.get("bundle-info")
    if not isinstance(bundle_info, Mapping) or bundle_info.get("bundle-name") != bundle_name:
        return False
    # Some Profile versions include appId at the root.  If present, it is
    # another canonical identity check; absence is allowed because bundle-name
    # is the portable field emitted by every supported Profile version.
    root_app_id = payload.get("appId")
    return root_app_id in (None, app_id)


def _as_certificate(value: Any) -> Optional[Any]:
    _require_crypto()
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            raw = value.encode("ascii")
        except UnicodeEncodeError:
            return None
    else:
        return None
    try:
        if b"BEGIN CERTIFICATE" in raw:
            return x509.load_pem_x509_certificate(raw)
        try:
            return x509.load_der_x509_certificate(raw)
        except ValueError:
            return x509.load_der_x509_certificate(base64.b64decode(raw, validate=True))
    except (ValueError, TypeError):
        return None


def _profile_has_certificate(payload: Any, fingerprint: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    bundle_info = payload.get("bundle-info")
    if not isinstance(bundle_info, Mapping):
        return False
    value = bundle_info.get("distribution-certificate")
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        certificate = _as_certificate(item)
        if certificate is not None and _fingerprint(certificate) == fingerprint:
            return True
    return False


def _parse_profile_time(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for form in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(value.strip(), form)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)


def _profile_validity_ok(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    validity = payload.get("validity")
    if not isinstance(validity, Mapping):
        return False
    now = dt.datetime.now(dt.timezone.utc)
    not_before = _parse_profile_time(validity.get("not-before"))
    not_after = _parse_profile_time(validity.get("not-after"))
    return not_before is not None and not_after is not None and not_before <= now <= not_after


def verify_profile(
    profile_path: Path,
    *,
    bundle_name: str,
    app_id: str,
    certificate_fingerprint: str,
    app_gallery: bool = True,
    openssl_runner: Callable[[Path, Path], bytes] = _openssl_profile_payload,
) -> None:
    payload_path = profile_path.with_suffix(".payload")
    try:
        payload = _parse_profile_payload(openssl_runner(profile_path, payload_path))
    finally:
        payload_path.unlink(missing_ok=True)
    if not _profile_type_ok(payload):
        raise ProvisionProtocolError("Profile is not a release Profile")
    if not _profile_bundle_ok(payload, bundle_name, app_id):
        raise ProvisionProtocolError("Profile bundle identity does not match the requested bundle")
    if not _profile_has_certificate(payload, certificate_fingerprint):
        raise ProvisionProtocolError("Profile does not contain the downloaded distribution certificate")
    if not _profile_validity_ok(payload):
        raise ProvisionProtocolError("Profile is outside its validity period")
    if app_gallery:
        if not isinstance(payload, Mapping) or payload.get("app-distribution-type") != "app_gallery":
            raise ProvisionProtocolError("Profile is missing the requested AppGallery distribution marker")


def _finish_profile(
    state: Dict[str, Any],
    paths: Mapping[str, Path],
    state_path: Path,
    transport: Any,
    *,
    bundle_name: str,
    app_id: str,
    app_gallery: bool,
) -> None:
    if state.get("profilePhase") == "verified":
        _require_mode(paths["profile"])
        verify_profile(
            paths["profile"],
            bundle_name=bundle_name,
            app_id=app_id,
            certificate_fingerprint=str(state["certFingerprint"]),
            app_gallery=app_gallery,
        )
        return
    if state.get("profilePhase") != "download_pending":
        raise ProvisionUncertain("Profile is not in a recoverable download state")
    url = state.get("profileDownloadUrl")
    if not isinstance(url, str):
        raise ProvisionUncertain("Profile download URL is unavailable; do not create another Profile")
    data = _download(transport, url)
    _atomic_write(paths["profile"], data, 0o600)
    try:
        verify_profile(
            paths["profile"],
            bundle_name=bundle_name,
            app_id=app_id,
            certificate_fingerprint=str(state["certFingerprint"]),
            app_gallery=app_gallery,
        )
    except BaseException:
        paths["profile"].unlink(missing_ok=True)
        raise
    state.update({"profilePhase": "verified", "lastError": None})
    _write_state(state_path, state)


def _parse_json5_file(path: Path) -> Dict[str, Any]:
    try:
        import json5  # type: ignore

        value = json5.loads(path.read_text(encoding="utf-8"))
    except ImportError:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvisionError("build-profile.json5 requires json5 on this Python") from exc
    except (OSError, ValueError) as exc:
        raise ProvisionError("build-profile.json5 is not valid JSON5") from exc
    if not isinstance(value, dict):
        raise ProvisionError("build-profile.json5 must contain an object")
    return value


def _material(config: Mapping[str, Any]) -> Dict[str, Any]:
    existing = config.get("material")
    if isinstance(existing, Mapping):
        return dict(existing)
    return {}


def _find_password_encryptor(
    project_path: Path,
    explicit_path: Optional[os.PathLike[str] | str] = None,
) -> Path:
    """Find the helper that emits the SDK's encrypted material password.

    A project-local helper takes precedence so a project can use the exact
    DevEco SDK version installed for that project.  The bundled helper is the
    fallback used by the skill installation.  The helper's stdout is consumed
    privately by this module and is never included in an error message.
    """

    candidates = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path).expanduser())
    candidates.extend(
        (
            project_path / "scripts" / ENCRYPTOR_FILE_NAME,
            project_path.parent / "scripts" / ENCRYPTOR_FILE_NAME,
            Path(__file__).resolve().with_name(ENCRYPTOR_FILE_NAME),
        )
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    raise ProvisionError("SDK signing password encryption helper is unavailable")


def _encrypt_config_password(
    project_path: Path,
    signing_directory: Path,
    password: str,
    *,
    encryptor_path: Optional[os.PathLike[str] | str] = None,
) -> str:
    """Run the local Node helper and return an SDK-compatible password value.

    The actual password is sent over stdin, and both stdout and diagnostics
    stay inside this process.  Errors intentionally omit subprocess output so
    a broken helper cannot leak the password or encrypted material.
    """

    helper = _find_password_encryptor(project_path, encryptor_path)
    request = _json_bytes({"store_dir": str(signing_directory), "password": password})
    try:
        result = subprocess.run(
            ["node", str(helper)],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError("SDK signing password encryption helper failed") from exc
    if result.returncode != 0:
        raise ProvisionError("SDK signing password encryption helper failed")
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("SDK signing password encryption helper returned invalid data") from exc
    encrypted = response.get("encrypted_password") if isinstance(response, Mapping) else None
    if (
        not isinstance(encrypted, str)
        or not encrypted
        or len(encrypted) > 8192
        or any(character in encrypted for character in "\x00\r\n")
        or encrypted == password
    ):
        raise ProvisionError("SDK signing password encryption helper returned invalid data")
    return encrypted


def _project_configuration_source(
    document: Mapping[str, Any],
    *,
    product: str,
) -> Tuple[Mapping[str, Any], list, list]:
    """Validate the selected product and return its source config.

    This check is deliberately side-effect free so callers can run it before
    making any remote certificate/Profile request.
    """

    app = document.get("app")
    if not isinstance(app, Mapping):
        raise ProvisionError("build-profile.json5 has no app object")
    configs = app.get("signingConfigs")
    products = app.get("products")
    if not isinstance(configs, list) or not isinstance(products, list):
        raise ProvisionError("build-profile.json5 has no signingConfigs/products arrays")
    selected_product = next(
        (item for item in products if isinstance(item, Mapping) and item.get("name") == product),
        None,
    )
    if selected_product is None:
        raise ProvisionError(f"product {product} is not present in build-profile.json5")
    if not any(isinstance(item, Mapping) for item in configs):
        raise ProvisionError("build-profile.json5 has no existing signing configuration to copy")
    source_name = selected_product.get("signingConfig")
    source = next(
        (item for item in configs if isinstance(item, Mapping) and item.get("name") == source_name),
        None,
    )
    if source is None:
        source = next((item for item in configs if isinstance(item, Mapping)), None)
    if source is None:
        raise ProvisionError("build-profile.json5 has no existing signing configuration to copy")
    return source, configs, products


def _path_value_matches(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).expanduser().resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return False


def _config_matches_material(
    config: Mapping[str, Any],
    *,
    paths: Mapping[str, Path],
    key_alias: str,
) -> bool:
    material = config.get("material")
    if not isinstance(material, Mapping):
        return False
    return (
        material.get("keyAlias") == key_alias
        and _path_value_matches(material.get("certpath"), paths["certificate"])
        and _path_value_matches(material.get("profile"), paths["profile"])
        and _path_value_matches(material.get("storeFile"), paths["pkcs12"])
    )


def _preflight_configuration(
    project: os.PathLike[str] | str,
    *,
    product: str,
    signing_directory: Path,
    key_alias: str,
    configured_config_name: Optional[str] = None,
) -> None:
    """Check project configuration before a remote mutation is attempted."""

    project_path = Path(project).expanduser()
    profile_path = project_path / "build-profile.json5"
    if not profile_path.is_file():
        raise ProvisionError("project has no build-profile.json5")
    document = _parse_json5_file(profile_path)
    _project_configuration_source(document, product=product)
    app = document["app"]
    configs = app["signingConfigs"]
    existing = next(
        (item for item in configs if isinstance(item, Mapping) and item.get("name") == (configured_config_name or key_alias)),
        None,
    )
    if configured_config_name:
        if existing is not None and not _config_matches_material(existing, paths=_paths(signing_directory), key_alias=key_alias):
            raise ProvisionError("stored signing configuration no longer matches the local signing identity")
    elif existing is not None:
        # There is no durable state binding this name to our identity.  Do not
        # replace a user's existing entry or silently change another product.
        raise ProvisionError(f"signing configuration {key_alias} already exists")


def configure_project(
    project: os.PathLike[str] | str,
    *,
    product: str,
    signing_directory: Path,
    key_alias: str = "agc-release",
    dry_run: bool = False,
    configured_config_name: Optional[str] = None,
    password_encryptor: Optional[PasswordEncryptor] = None,
    encryptor_path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    project_path = Path(project).expanduser()
    profile_path = project_path / "build-profile.json5"
    if not profile_path.is_file():
        raise ProvisionError("project has no build-profile.json5")
    document = _parse_json5_file(profile_path)
    source, configs, products = _project_configuration_source(document, product=product)
    app = document["app"]
    paths = _paths(signing_directory)
    target_name = configured_config_name or key_alias
    existing_target = next(
        (item for item in configs if isinstance(item, Mapping) and item.get("name") == target_name),
        None,
    )
    if configured_config_name:
        if existing_target is not None and not _config_matches_material(
            existing_target, paths=paths, key_alias=key_alias
        ):
            raise ProvisionError("stored signing configuration no longer matches the local signing identity")
    elif existing_target is not None:
        # A fresh state has no evidence that a pre-existing name belongs to
        # this identity.  Refuse to overwrite it, preserving other products
        # that may still reference that configuration.
        raise ProvisionError(f"signing configuration {key_alias} already exists")
    if dry_run:
        return {"product": product, "signingConfig": target_name, "updated": False}
    new_config = dict(source)
    new_config["name"] = target_name
    material = _material(source)
    try:
        password = _read_password(paths["password"]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvisionError("private key password file is not UTF-8") from exc
    if password_encryptor is None:
        encrypted_password = _encrypt_config_password(
            project_path,
            signing_directory,
            password,
            encryptor_path=encryptor_path,
        )
    else:
        try:
            encrypted_password = password_encryptor(signing_directory, password)
        except ProvisionError:
            raise
        except Exception as exc:
            raise ProvisionError("SDK signing password encryption helper failed") from exc
        if (
            not isinstance(encrypted_password, str)
            or not encrypted_password
            or len(encrypted_password) > 8192
            or any(character in encrypted_password for character in "\x00\r\n")
            or encrypted_password == password
        ):
            raise ProvisionError("SDK signing password encryption helper returned invalid data")
    material.update(
        {
            "certpath": str(paths["certificate"]),
            "keyAlias": key_alias,
            "keyPassword": encrypted_password,
            "profile": str(paths["profile"]),
            "storeFile": str(paths["pkcs12"]),
            "storePassword": encrypted_password,
        }
    )
    new_config["material"] = material
    updated = dict(document)
    updated_app = dict(app)
    updated_configs = [dict(item) if isinstance(item, Mapping) else item for item in configs]
    replaced = False
    for index, item in enumerate(updated_configs):
        if isinstance(item, Mapping) and item.get("name") == target_name:
            updated_configs[index] = new_config
            replaced = True
            break
    if not replaced:
        updated_configs.append(new_config)
    updated_products = [dict(item) if isinstance(item, Mapping) else item for item in products]
    for index, item in enumerate(updated_products):
        if isinstance(item, Mapping) and item.get("name") == product:
            item["signingConfig"] = target_name
            updated_products[index] = item
            break
    updated_app["signingConfigs"] = updated_configs
    updated_app["products"] = updated_products
    updated["app"] = updated_app
    if not dry_run:
        # The configured material contains the keystore password.  Restrict
        # the project profile itself to the owner after this mutation.
        _atomic_write(profile_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)
    return {"product": product, "signingConfig": target_name, "updated": not dry_run}


def _base_state(paths: Mapping[str, Path], bundle_name: str, app_id: str, product: str) -> Dict[str, Any]:
    state = _read_state(paths["state"])
    _validate_state(state, paths, bundle_name, app_id, product)
    return state


def _provision_release_locked(
    *,
    project: os.PathLike[str] | str,
    app_id: str,
    bundle_name: str,
    product: str = "default",
    credentials_path: Optional[os.PathLike[str] | str] = None,
    signing_root: os.PathLike[str] | str = DEFAULT_SIGNING_ROOT,
    stage: str = "all",
    create_signing: bool = False,
    reuse_only: bool = True,
    dry_run: bool = False,
    cert_name: Optional[str] = None,
    provision_name: Optional[str] = None,
    password_file: Optional[os.PathLike[str] | str] = None,
    app_gallery: bool = True,
    transport: Optional[Any] = None,
    password_encryptor: Optional[PasswordEncryptor] = None,
    encryptor_path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    _safe_bundle_name(bundle_name)
    if create_signing and reuse_only:
        raise ProvisionError("create-signing and reuse-only are mutually exclusive")
    if not create_signing and not reuse_only:
        reuse_only = True
    if stage not in {"prepare", "provision", "configure", "all"}:
        raise ProvisionError("stage must be prepare, provision, configure, or all")
    root = Path(signing_root).expanduser()
    directory = root / bundle_name
    state_path = _state_path(directory)
    has_state = state_path.is_file()
    if dry_run:
        return {
            "status": "plan",
            "wouldCreateSigning": bool(create_signing and not has_state),
            "bundleName": bundle_name,
            "appId": app_id,
            "product": product,
            "localDirectory": str(directory),
        }
    if stage in {"provision", "all"} and not has_state:
        # This variant does not need the generated key material, so it can
        # reject an empty project or a colliding name before creating local
        # state or attempting a remote mutation.
        _preflight_configuration(
            project,
            product=product,
            signing_directory=directory,
            key_alias="agc-release",
        )
    _require_crypto()
    if has_state:
        paths = _existing_directory(root, bundle_name)
        state = _base_state(paths, bundle_name, app_id, product)
    else:
        if reuse_only:
            raise ProvisionError("reuse-only requested but no local signing state exists")
        paths = _new_directory(root, bundle_name)
        supplied_password = None
        if password_file is not None:
            supplied_password = _read_password(Path(password_file).expanduser())
        state = _generate_material(paths, bundle_name, supplied_password)
        state["appId"] = app_id
        state["product"] = product
        _write_state(paths["state"], state)
    if password_file is not None and Path(password_file).expanduser() != paths["password"]:
        # Existing state never silently changes the key password source.
        supplied = Path(password_file).expanduser()
        if _read_password(supplied) != _read_password(paths["password"]):
            raise ProvisionError("provided password file does not match existing signing state")
    if stage == "prepare":
        return {
            "status": "prepared",
            "publicFingerprint": None,
            "localDirectory": str(paths["directory"]),
        }
    # The API mutations below are only useful if the local project can accept
    # the resulting identity.  Validate the target before the first POST so a
    # missing/empty configuration or an unrelated name collision cannot leave
    # an orphaned AGC certificate/Profile.
    if stage in {"provision", "all"} and has_state:
        _preflight_configuration(
            project,
            product=product,
            signing_directory=paths["directory"],
            key_alias="agc-release",
            configured_config_name=state.get("configName"),
        )
    if stage in {"provision", "all"}:
        credentials = _load_credentials(credentials_path)
        client = _client(credentials, transport)
        net = transport or agc_upload.UrllibTransport()
        _request_cert(
            client,
            state,
            paths["state"],
            cert_name=cert_name or f"{bundle_name}-release-cert",
            allow_create=create_signing and not reuse_only,
        )
        _finish_certificate(state, paths, paths["state"], net)
        _request_profile(
            client,
            state,
            paths["state"],
            provision_name=provision_name or f"{bundle_name}-release-profile",
            app_id=app_id,
            allow_create=create_signing and not reuse_only,
        )
        _finish_profile(
            state,
            paths,
            paths["state"],
            net,
            bundle_name=bundle_name,
            app_id=app_id,
            app_gallery=app_gallery,
        )
    if stage in {"configure", "all"}:
        if state.get("certPhase") != "verified" or state.get("profilePhase") != "verified":
            raise ProvisionError("verified certificate and Profile are required before configure")
        configured = configure_project(
            project,
            product=product,
            signing_directory=paths["directory"],
            configured_config_name=state.get("configName"),
            password_encryptor=password_encryptor,
            encryptor_path=encryptor_path,
            dry_run=False,
        )
        state["configName"] = configured["signingConfig"]
        state["configured"] = True
        state["lastError"] = None
        _write_state(paths["state"], state)
    return {
        "status": "ready" if state.get("configured") else "provisioned",
        "publicFingerprint": state.get("certFingerprint"),
        "localDirectory": str(paths["directory"]),
    }


class IdentityLock:
    """Stable per-bundle lock held across state, API, and config mutations."""

    def __init__(self, path: Path, *, create_root: bool) -> None:
        self.path = path
        self.create_root = create_root
        self.fd: Optional[int] = None

    def acquire(self) -> None:
        if fcntl is None:
            raise ProvisionError("signing identity locking is unavailable on this platform")
        if self.create_root:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        elif not self.path.parent.is_dir():
            raise ProvisionError("reuse-only requested but no signing state exists")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            raise ProvisionError("signing identity is already in use") from exc
        except OSError as exc:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            raise ProvisionError("cannot lock signing identity") from exc
        self.fd = fd

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
        finally:
            os.close(fd)


def provision_release(
    *,
    project: os.PathLike[str] | str,
    app_id: str,
    bundle_name: str,
    product: str = "default",
    credentials_path: Optional[os.PathLike[str] | str] = None,
    signing_root: os.PathLike[str] | str = DEFAULT_SIGNING_ROOT,
    stage: str = "all",
    create_signing: bool = False,
    reuse_only: bool = True,
    dry_run: bool = False,
    cert_name: Optional[str] = None,
    provision_name: Optional[str] = None,
    password_file: Optional[os.PathLike[str] | str] = None,
    app_gallery: bool = True,
    transport: Optional[Any] = None,
    password_encryptor: Optional[PasswordEncryptor] = None,
    encryptor_path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    if dry_run:
        # Planning performs no state/API mutation and therefore does not need
        # to create or acquire the per-identity lock directory.
        return _provision_release_locked(
            project=project,
            app_id=app_id,
            bundle_name=bundle_name,
            product=product,
            credentials_path=credentials_path,
            signing_root=signing_root,
            stage=stage,
            create_signing=create_signing,
            reuse_only=reuse_only,
            dry_run=True,
            cert_name=cert_name,
            provision_name=provision_name,
            password_file=password_file,
            app_gallery=app_gallery,
            transport=transport,
            password_encryptor=password_encryptor,
            encryptor_path=encryptor_path,
        )
    root = Path(signing_root).expanduser()
    safe_bundle = _safe_bundle_name(bundle_name)
    lock = IdentityLock(root / f".{safe_bundle}.lock", create_root=not reuse_only)
    lock.acquire()
    try:
        return _provision_release_locked(
            project=project,
            app_id=app_id,
            bundle_name=bundle_name,
            product=product,
            credentials_path=credentials_path,
            signing_root=signing_root,
            stage=stage,
            create_signing=create_signing,
            reuse_only=reuse_only,
            dry_run=dry_run,
            cert_name=cert_name,
            provision_name=provision_name,
            password_file=password_file,
            app_gallery=app_gallery,
            transport=transport,
            password_encryptor=password_encryptor,
            encryptor_path=encryptor_path,
        )
    finally:
        lock.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and provision a reusable AGC release signing identity")
    parser.add_argument("--project", required=True, help="HarmonyOS project root")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--bundle-name", required=True)
    parser.add_argument("--product", default="default")
    parser.add_argument("--credentials", metavar="PATH")
    parser.add_argument("--signing-root", metavar="PATH", default=str(DEFAULT_SIGNING_ROOT))
    parser.add_argument("--stage", choices=("prepare", "provision", "configure", "all"), default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--create-signing", action="store_true")
    mode.add_argument("--reuse-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cert-name")
    parser.add_argument("--provision-name")
    parser.add_argument("--password-file", metavar="PATH")
    parser.add_argument("--app-gallery", action="store_true", default=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = provision_release(
            project=args.project,
            app_id=args.app_id,
            bundle_name=args.bundle_name,
            product=args.product,
            credentials_path=args.credentials,
            signing_root=args.signing_root,
            stage=args.stage,
            create_signing=args.create_signing,
            reuse_only=args.reuse_only or not args.create_signing,
            dry_run=args.dry_run,
            cert_name=args.cert_name,
            provision_name=args.provision_name,
            password_file=args.password_file,
            app_gallery=args.app_gallery,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "error": "interrupted"}, ensure_ascii=False))
        return 130
    except (ProvisionError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
