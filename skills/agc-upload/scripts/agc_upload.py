#!/usr/bin/env python3
"""Small, non-GUI AppGallery Connect uploader for HarmonyOS ``.app`` files.

The command deliberately keeps the API surface narrow.  Package inspection is
fully offline; network operations use an injectable transport in tests and a
redirect-free urllib transport in normal use.  The receipt contains only the
state needed to continue a known upload and never contains pre-signed URLs,
request headers, access tokens, or private keys.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path
import posixpath
import re
import socket
import stat
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPSConnection, IncompleteRead
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

try:
    import fcntl
except ImportError:  # pragma: no cover - supported hosts are POSIX.
    fcntl = None  # type: ignore[assignment]


AGC_BASE_URL = "https://connect-api.cloud.huawei.com"
SERVICE_ACCOUNT_AUDIENCE = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "agc-upload" / "credentials.json"
DEFAULT_RECEIPT_DIR = Path.home() / ".cache" / "agc-upload" / "receipts"
DEFAULT_POLL_TIMEOUT = 600.0
DEFAULT_POLL_INTERVAL = 10.0
MAX_METADATA_BYTES = 8 * 1024 * 1024

try:  # Optional at import time so inspect works on a stock Python install.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the stock-Python path.
    hashes = serialization = padding = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False


class AgcError(Exception):
    """Base class for expected command failures."""


class PackageError(AgcError):
    pass


class CredentialsError(AgcError):
    pass


class ReceiptError(AgcError):
    pass


class ProtocolError(AgcError):
    pass


class NetworkError(AgcError):
    """Transport failure; the remote outcome may be unknown for mutations."""


class RedirectError(NetworkError):
    pass


class HttpResponseError(AgcError):
    def __init__(self, status: int, *, mutation: bool = False) -> None:
        self.status = int(status)
        self.mutation = mutation
        super().__init__(f"AGC returned HTTP {self.status}")


class ApiCodeError(AgcError):
    def __init__(self, code: Any) -> None:
        self.code = code
        super().__init__(f"AGC returned ret.code {code}")


class UnsafeResumeError(AgcError):
    pass


class PollFailure(AgcError):
    def __init__(self, package_id: str, state: Any) -> None:
        self.package_id = package_id
        self.state = state
        super().__init__(f"package {package_id} compile failed (state {state})")


class PollTimeout(AgcError):
    def __init__(self, package_id: str) -> None:
        self.package_id = package_id
        super().__init__(f"package {package_id} remains pending after the poll timeout")


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    file_name: str
    size: int
    sha256: str
    bundle_name: str
    version_name: str
    version_code: Any
    build_version: Any
    metadata_release: Any
    modules: Tuple[Mapping[str, Any], ...]
    file_identity: FileIdentity
    pack_info: Mapping[str, Any]


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise CredentialsError("malformed JWT segment")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _walk_key_values(value: Any) -> Iterable[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_key_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_key_values(item)


def _metadata_value(value: Any, *names: str) -> Any:
    wanted = {n.lower() for n in names}
    for key, item in _walk_key_values(value):
        if key.lower() in wanted:
            return item
    return None


def _parse_json_metadata(data: bytes, description: str) -> Mapping[str, Any]:
    if len(data) > MAX_METADATA_BYTES:
        raise PackageError(f"{description} is too large")
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # module.json5 is optional metadata.  Use json5 if the host has it,
        # while keeping pack.info strict and dependency-free.
        if description.endswith("module.json5"):
            try:
                import json5  # type: ignore

                result = json5.loads(data.decode("utf-8"))
            except Exception:
                raise PackageError(f"invalid {description}") from exc
        else:
            raise PackageError(f"invalid {description}") from exc
    if not isinstance(result, Mapping):
        raise PackageError(f"{description} must contain a JSON object")
    return result


def _validate_zip_member(name: str) -> None:
    if "\x00" in name:
        raise PackageError("package contains an invalid ZIP member name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or posixpath.normpath(normalized).startswith("../"):
        raise PackageError("package contains a path-traversal ZIP member")


def _snapshot(path: Path) -> FileIdentity:
    try:
        info = path.stat()
    except OSError as exc:
        raise PackageError(f"cannot stat package: {path.name}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackageError("package path is not a regular file")
    return FileIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        ctime_ns=int(info.st_ctime_ns),
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise PackageError(f"cannot read package: {path.name}") from exc
    return digest.hexdigest()


def _ensure_unchanged(info: PackageInfo) -> None:
    if _snapshot(info.path) != info.file_identity:
        raise PackageError("package file identity changed")
    current_hash = _hash_file(info.path)
    if current_hash != info.sha256:
        raise PackageError("package SHA-256 changed")


def _extract_app_version(pack_info: Mapping[str, Any]) -> Tuple[str, str, Any, Any]:
    summary = pack_info.get("summary")
    if not isinstance(summary, Mapping):
        raise PackageError("pack.info is missing summary.app")
    app = summary.get("app")
    if not isinstance(app, Mapping):
        raise PackageError("pack.info is missing summary.app")
    bundle_name = _first_value(app, "bundleName", "bundle_name")
    version = _first_value(app, "version")
    if not isinstance(bundle_name, str) or not bundle_name.strip():
        raise PackageError("pack.info summary.app.bundleName is missing")
    version_name: Any = None
    version_code: Any = None
    if isinstance(version, Mapping):
        version_name = _first_value(version, "name", "versionName")
        version_code = _first_value(version, "code", "versionCode")
    elif isinstance(version, (str, int, float)):
        version_name = str(version)
    if version_name is None:
        version_name = _first_value(app, "versionName", "version_name")
    if version_code is None:
        version_code = _first_value(app, "versionCode", "version_code")
    if not isinstance(version_name, str) or not version_name.strip():
        raise PackageError("pack.info summary.app.version.name is missing")
    return bundle_name.strip(), version_name.strip(), version_code, version


def _reject_debug_or_test_metadata(pack_info: Mapping[str, Any]) -> None:
    rejected = {"debug", "isdebug", "debuggable", "test", "istest", "testing", "istesting"}
    release_keys = {
        "buildrelease",
        "build_release",
        "buildmode",
        "build_mode",
        "releasebuild",
        "isrelease",
        "releasetype",
    }
    for key, value in _walk_key_values(pack_info):
        lowered = key.lower().replace("-", "_")
        if lowered in rejected and (value is True or str(value).strip().lower() in {"true", "debug", "test", "testing"}):
            raise PackageError(f"package metadata identifies a {lowered} build")
        if lowered in release_keys:
            if value is False or str(value).strip().lower() in {"false", "0", "debug", "test", "testing"}:
                raise PackageError("package metadata does not identify a release build")


def _inspect_inner_module(data: bytes, member_name: str) -> Mapping[str, Any]:
    result: Dict[str, Any] = {"file": member_name}
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as module_zip:
            module_json_name = None
            for entry in module_zip.infolist():
                _validate_zip_member(entry.filename)
                base = posixpath.basename(entry.filename).lower()
                if base in {"module.json", "module.json5"}:
                    module_json_name = entry.filename
                    break
            if module_json_name is not None:
                metadata = _parse_json_metadata(
                    module_zip.read(module_json_name), f"{member_name}:{posixpath.basename(module_json_name)}"
                )
                _reject_debug_or_test_metadata(metadata)
                # Harmony module.json keeps the module identity under the
                # explicit ``module`` object.  Do not recursively search for
                # an arbitrary ``name``: permissions and other declarations
                # also contain name fields (for example INTERNET).
                module_section = metadata.get("module")
                if isinstance(module_section, Mapping):
                    result["moduleName"] = _first_value(module_section, "name", "moduleName")
                    result["moduleType"] = _first_value(module_section, "type", "moduleType")
                else:
                    # Compatibility shape used by some FA packages.
                    result["moduleName"] = _first_value(metadata, "moduleName")
                    result["moduleType"] = _first_value(metadata, "moduleType")
                nested_bundle = _metadata_value(metadata, "bundleName", "bundle_name")
                if nested_bundle is not None:
                    result["bundleName"] = nested_bundle
                nested_build = _metadata_value(metadata, "buildVersion", "build_version")
                if nested_build is not None:
                    result["buildVersion"] = nested_build
                nested_release = _metadata_value(metadata, "metadataRelease", "releaseType")
                if nested_release is not None:
                    result["metadataRelease"] = nested_release
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(f"inner {member_name} is not a readable ZIP module") from exc
    return result


def inspect_package(package: os.PathLike[str] | str) -> PackageInfo:
    path = Path(package).expanduser()
    if path.suffix.lower() != ".app":
        raise PackageError("package must have a .app extension")
    before = _snapshot(path)
    digest = _hash_file(path)
    after_hash = _snapshot(path)
    if before != after_hash:
        raise PackageError("package file identity changed while hashing")
    pack_info: Mapping[str, Any]
    modules = []
    try:
        with zipfile.ZipFile(path, "r") as package_zip:
            entries = package_zip.infolist()
            if not entries:
                raise PackageError("package ZIP is empty")
            for entry in entries:
                _validate_zip_member(entry.filename)
            pack_entries = [entry for entry in entries if entry.filename == "pack.info"]
            if len(pack_entries) != 1:
                raise PackageError("package must contain exactly one root pack.info")
            pack_info = _parse_json_metadata(package_zip.read(pack_entries[0]), "pack.info")
            bundle_name, version_name, version_code, _ = _extract_app_version(pack_info)
            _reject_debug_or_test_metadata(pack_info)
            module_entries = [
                entry
                for entry in entries
                if entry.filename.lower().endswith((".hap", ".hsp"))
            ]
            if not module_entries:
                raise PackageError("package must contain at least one .hap or .hsp module")
            for entry in module_entries:
                if entry.file_size > MAX_METADATA_BYTES * 16:
                    raise PackageError(
                        f"cannot inspect release/debug metadata in large module {entry.filename}"
                    )
                modules.append(_inspect_inner_module(package_zip.read(entry), entry.filename))
    except zipfile.BadZipFile as exc:
        raise PackageError("package is not a valid ZIP archive") from exc
    metadata_release = _metadata_value(pack_info, "metadataRelease", "releaseType")
    build_version = _metadata_value(pack_info, "buildVersion", "build_version")
    if build_version is None:
        # Some generated pack.info files expose this under the app version.
        summary = pack_info.get("summary")
        if isinstance(summary, Mapping) and isinstance(summary.get("app"), Mapping):
            version = summary["app"].get("version")
            if isinstance(version, Mapping):
                build_version = _first_value(version, "buildVersion", "build_version")
    if build_version is None:
        for module in modules:
            if module.get("buildVersion") is not None:
                build_version = module["buildVersion"]
                break
    for module in modules:
        nested_bundle = module.get("bundleName")
        if nested_bundle is not None and nested_bundle != bundle_name:
            raise PackageError("inner module bundleName does not match pack.info")
    return PackageInfo(
        path=path,
        file_name=path.name,
        size=before.size,
        sha256=digest,
        bundle_name=bundle_name,
        version_name=version_name,
        version_code=version_code,
        build_version=build_version,
        metadata_release=metadata_release,
        modules=tuple(modules),
        file_identity=before,
        pack_info=pack_info,
    )


def package_info_json(info: PackageInfo) -> Dict[str, Any]:
    return {
        "fileName": info.file_name,
        "size": info.size,
        "sha256": info.sha256,
        "bundleName": info.bundle_name,
        "versionName": info.version_name,
        "versionCode": info.version_code,
        "buildVersion": info.build_version,
        "metadataRelease": info.metadata_release,
        "modules": list(info.modules),
        "signatureVerification": {
            "cryptographicallyVerified": False,
            "finalAuthority": "AppGallery Connect",
        },
    }


def _require_private_credentials_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CredentialsError("credentials file cannot be read") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise CredentialsError("credentials file must be a regular file with mode 0600")


def resolve_credentials_path(argument: Optional[os.PathLike[str] | str]) -> Optional[Path]:
    if argument:
        return Path(argument).expanduser()
    env_path = os.environ.get("AGC_CREDENTIALS_FILE")
    if env_path:
        return Path(env_path).expanduser()
    if DEFAULT_CREDENTIALS_PATH.exists():
        return DEFAULT_CREDENTIALS_PATH
    return None


def load_credentials(path: os.PathLike[str] | str) -> Mapping[str, Any]:
    credential_path = Path(path).expanduser()
    _require_private_credentials_file(credential_path)
    try:
        raw = credential_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialsError("credentials file is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise CredentialsError("credentials file must contain a JSON object")
    service_fields = ("key_id", "private_key", "sub_account")
    legacy_fields = ("client_id", "client_secret")
    has_service = all(isinstance(data.get(key), str) and data.get(key) for key in service_fields)
    has_legacy = all(isinstance(data.get(key), str) and data.get(key) for key in legacy_fields)
    if has_service and has_legacy:
        raise CredentialsError("credentials cannot contain both Service Account and API client fields")
    if not has_service and not has_legacy:
        raise CredentialsError("credentials must contain Service Account or client credentials")
    if has_service:
        token_uri = data.get("token_uri")
        if token_uri is not None and token_uri != SERVICE_ACCOUNT_AUDIENCE:
            raise CredentialsError("Service Account token_uri does not match Huawei's token audience")
        return dict(data, _kind="service")
    return dict(data, _kind="legacy")


def create_service_account_jwt(credentials: Mapping[str, Any], now: Optional[int] = None) -> str:
    if not _CRYPTO_AVAILABLE:
        raise CredentialsError("cryptography is required for Service Account credentials")
    try:
        key_id = str(credentials["key_id"])
        private_key_text = str(credentials["private_key"]).replace("\\n", "\n")
        sub_account = str(credentials["sub_account"])
    except (KeyError, TypeError) as exc:
        raise CredentialsError("incomplete Service Account credentials") from exc
    issued_at = int(time.time() if now is None else now)
    header = {"kid": key_id, "typ": "JWT", "alg": "PS256"}
    payload = {
        "aud": SERVICE_ACCOUNT_AUDIENCE,
        "iss": sub_account,
        "exp": issued_at + 3600,
        "iat": issued_at,
    }
    encoded_header = _b64url(_json_bytes(header))
    encoded_payload = _b64url(_json_bytes(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        private_key = serialization.load_pem_private_key(private_key_text.encode("utf-8"), password=None)
        signature = private_key.sign(
            signing_input,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise CredentialsError("Service Account private key could not create a PS256 JWT") from exc
    return f"{encoded_header}.{encoded_payload}.{_b64url(signature)}"


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        raise RedirectError("redirect refused")


class FileStream:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("rb")
        self.size = int(os.fstat(self._handle.fileno()).st_size)
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, amount: int = -1) -> bytes:
        chunk = self._handle.read(amount)
        if chunk:
            self.count += len(chunk)
            self.digest.update(chunk)
        return chunk

    def close(self) -> None:
        self._handle.close()


class UrllibTransport:
    """Redirect-free HTTPS transport used outside tests."""

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        *,
        body: Optional[bytes] = None,
        stream: Optional[FileStream] = None,
    ) -> HttpResponse:
        if body is not None and stream is not None:
            raise ValueError("body and stream are mutually exclusive")
        if stream is not None:
            return self._raw_stream_request(method, url, headers or {}, stream)
        request = Request(url, data=body if body is not None else stream, headers=dict(headers or {}), method=method)
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=60) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers={str(k): str(v) for k, v in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as exc:
            try:
                response_body = exc.read()
            except Exception:
                response_body = b""
            return HttpResponse(status=int(exc.code), headers=dict(exc.headers.items()), body=response_body)
        except RedirectError:
            raise
        except (URLError, OSError, IncompleteRead, TimeoutError) as exc:
            raise NetworkError("HTTPS request failed") from exc

    @staticmethod
    def _raw_stream_request(
        method: str, url: str, headers: Mapping[str, str], stream: FileStream
    ) -> HttpResponse:
        """Send one raw PUT without urllib's form/multipart/default headers."""
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if not host:
                raise ValueError("missing host")
            port = parsed.port or 443
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection = HTTPSConnection(host, port, timeout=60, context=ssl.create_default_context())
            connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            header_names = {key.lower() for key in headers}
            for key, value in headers.items():
                connection.putheader(key, value)
            if "host" not in header_names:
                connection.putheader("Host", host)
            if "content-length" not in header_names:
                connection.putheader("Content-Length", str(stream.size))
            connection.endheaders()
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                connection.send(block)
            response = connection.getresponse()
            result = HttpResponse(
                status=int(response.status),
                headers={str(key): str(value) for key, value in response.getheaders()},
                body=response.read(),
            )
            connection.close()
            return result
        except (HTTPError, URLError, OSError, IncompleteRead, TimeoutError, ValueError) as exc:
            raise NetworkError("HTTPS upload failed") from exc


def _private_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def validate_upload_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        raise ProtocolError("AGC returned an invalid upload URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProtocolError("AGC returned an invalid upload URL") from exc
    if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None:
        raise ProtocolError("upload URL must use HTTPS without embedded credentials")
    if port not in (None, 443):
        raise ProtocolError("upload URL uses an unexpected port")
    host = host.rstrip(".").lower()
    if _private_ip(host):
        raise ProtocolError("upload URL resolves to a private IP address")
    allowed_suffixes = (".myhuaweicloud.com", ".myhuaweicloud.cn", ".myhwclouds.com", ".huawei.com")
    if not any(host.endswith(suffix) for suffix in allowed_suffixes):
        raise ProtocolError("upload URL host is not an approved Huawei object-storage domain")
    return url


def _ret_code(payload: Mapping[str, Any]) -> Any:
    ret = payload.get("ret")
    if not isinstance(ret, Mapping) or "code" not in ret:
        raise ProtocolError("AGC response is missing ret.code")
    return ret.get("code")


def _is_zero(code: Any) -> bool:
    return code == 0 or code == "0"


def _validate_safe_identifier(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value or "://" in value:
        raise ProtocolError(f"AGC response has an invalid {description}")
    if len(value) > 1024:
        raise ProtocolError(f"AGC response has an oversized {description}")
    return value


class Authenticator:
    def __init__(self, credentials: Mapping[str, Any], transport: Any) -> None:
        self.credentials = credentials
        self.transport = transport
        self._token: Optional[str] = None
        self._jwt: Optional[str] = None

    @property
    def kind(self) -> str:
        return str(self.credentials.get("_kind"))

    def _legacy_token(self) -> str:
        if self._token:
            return self._token
        body = {
            "grant_type": "client_credentials",
            "client_id": self.credentials["client_id"],
            "client_secret": self.credentials["client_secret"],
        }
        response = self.transport.request(
            "POST",
            f"{AGC_BASE_URL}/api/oauth2/v1/token",
            {"Content-Type": "application/json"},
            body=_json_bytes(body),
        )
        if response.status != 200:
            raise HttpResponseError(response.status)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("token response is not valid JSON") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise ProtocolError("token response has no access_token")
        self._token = payload["access_token"]
        return self._token

    def headers(self) -> Dict[str, str]:
        if self.kind == "service":
            if self._jwt is None:
                self._jwt = create_service_account_jwt(self.credentials)
            return {"Authorization": f"Bearer {self._jwt}"}
        token = self._legacy_token()
        return {"Authorization": f"Bearer {token}", "client_id": str(self.credentials["client_id"])}


class AgcClient:
    def __init__(
        self,
        credentials: Mapping[str, Any],
        *,
        transport: Optional[Any] = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.auth = Authenticator(credentials, self.transport)

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        mutation: bool = False,
    ) -> Mapping[str, Any]:
        url = f"{AGC_BASE_URL}{path}"
        if query:
            url += "?" + urlencode([(key, value) for key, value in query.items() if value is not None])
        headers = self.auth.headers()
        request_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            request_body = _json_bytes(body)
        try:
            response = self.transport.request(method, url, headers, body=request_body)
        except (NetworkError, RedirectError):
            raise
        if response.status != 200:
            raise HttpResponseError(response.status, mutation=mutation)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("AGC response is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProtocolError("AGC response must be a JSON object")
        code = _ret_code(payload)
        if not _is_zero(code):
            raise ApiCodeError(code)
        return payload

    def get_upload_url(
        self,
        app_id: str,
        file_name: str,
        content_length: int,
        sha256: str,
        mainland: Optional[int],
    ) -> Tuple[str, str, Mapping[str, str]]:
        query: Dict[str, Any] = {
            "appId": app_id,
            "fileName": file_name,
            "contentLength": content_length,
            "sha256": sha256,
        }
        if mainland is not None:
            query["chineseMainlandFlag"] = mainland
        payload = self._api_request("GET", "/api/publish/v2/upload-url/for-obs", query=query)
        info = payload.get("urlInfo")
        if not isinstance(info, Mapping):
            raise ProtocolError("upload-url response is missing urlInfo")
        object_id = info.get("objectId")
        url = info.get("url")
        method = info.get("method")
        headers = info.get("headers", {})
        object_id = _validate_safe_identifier(object_id, "objectId")
        if not isinstance(method, str) or method.upper() != "PUT":
            raise ProtocolError("upload-url response method must be PUT")
        if not isinstance(headers, Mapping):
            raise ProtocolError("upload-url response headers must be an object")
        safe_headers: Dict[str, str] = {}
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\r" in key + value or "\n" in key + value:
                raise ProtocolError("upload-url response contains invalid request headers")
            safe_headers[key] = value
        if not isinstance(url, str):
            raise ProtocolError("upload-url response is missing url")
        return object_id, validate_upload_url(url), safe_headers

    def upload_file(self, info: PackageInfo, url: str, headers: Mapping[str, str]) -> None:
        stream = FileStream(info.path)
        upload_headers = dict(headers)
        # Content-Length describes the raw PUT body and is transport metadata,
        # not an AGC authentication header.  Never merge API auth headers here.
        if not any(key.lower() == "content-length" for key in upload_headers):
            upload_headers["Content-Length"] = str(info.size)
        try:
            response = self.transport.request("PUT", url, upload_headers, stream=stream)
        except (NetworkError, RedirectError):
            raise
        finally:
            stream.close()
        if stream.count != info.size or stream.digest.hexdigest() != info.sha256:
            raise PackageError("package changed while streaming upload")
        if response.status != 200:
            raise HttpResponseError(response.status, mutation=True)

    def register_package(self, app_id: str, file_name: str, object_id: str) -> str:
        payload = self._api_request(
            "PUT",
            "/api/publish/v3/app-package-info",
            query={"appId": app_id},
            body={"fileName": file_name, "objectId": object_id},
            mutation=True,
        )
        package_id = _validate_safe_identifier(payload.get("packageId"), "packageId")
        return package_id

    def register_testing(self, app_id: str, file_name: str, object_id: str) -> Any:
        payload = self._api_request(
            "POST",
            "/api/publish/v2/test/version/pkg",
            query={"appId": app_id},
            body={"distributeMode": 1, "file": {"fileName": file_name, "objectId": object_id}},
            mutation=True,
        )
        versions = payload.get("pkgVersion")
        if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], str):
            raise ProtocolError("testing registration response must contain one pkgVersion")
        # The testing API returns one package-version identifier.  Keep a
        # scalar identifier so it can be polled through compile/status.
        return _validate_safe_identifier(versions[0], "testing pkgVersion")

    def compile_status(self, app_id: str, package_id: str) -> int:
        payload = self._api_request(
            "GET",
            "/api/publish/v3/package/compile/status",
            query={"appId": app_id, "pkgIds": package_id},
        )
        states = payload.get("pkgStateList")
        if not isinstance(states, list):
            raise ProtocolError("compile status response is missing pkgStateList")
        match = None
        for item in states:
            if isinstance(item, Mapping) and item.get("pkgId") == package_id:
                match = item
                break
        if match is None or "successStatus" not in match:
            raise ProtocolError("compile status response is missing the requested package state")
        state = match.get("successStatus")
        if state not in (0, 1, 2):
            raise ProtocolError("compile status response has an unknown successStatus")
        return int(state)

    def lookup_app_ids(self, bundle_name: str) -> Sequence[Mapping[str, str]]:
        payload = self._api_request(
            "GET",
            "/api/publish/v2/appid-list",
            query={"packageName": bundle_name, "packageTypes": 7},
        )
        # The documented response field is lower-case ``appids`` (the key is
        # a display name such as PhotoPlaza, while value is the AppId).
        values = payload.get("appids")
        if values is None:  # tolerate the common camel-case spelling in test doubles.
            values = payload.get("appIds")
        if not isinstance(values, list):
            raise ProtocolError("app-id response is missing appids")
        result = []
        for item in values:
            if isinstance(item, Mapping) and isinstance(item.get("key"), str) and isinstance(item.get("value"), str):
                result.append({"displayName": item["key"], "appId": item["value"]})
        return tuple(result)


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and "://" in value:
            return "present"
        return value
    if isinstance(value, Mapping):
        # Keep only obvious identifiers from pkgVersion; never persist URLs or
        # arbitrary response objects.
        for key in ("pkgId", "versionId", "id", "versionName", "name"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)):
                return candidate
        return "present"
    return "present"


_RECEIPT_FORBIDDEN_KEYS = {
    "url",
    "urls",
    "headers",
    "header",
    "authorization",
    "token",
    "accesstoken",
    "clientsecret",
    "privatekey",
    "secret",
}


def _assert_receipt_safe(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).replace("_", "").replace("-", "").lower()
            if lowered in _RECEIPT_FORBIDDEN_KEYS:
                raise ReceiptError("receipt contains a forbidden credential or URL field")
            _assert_receipt_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_receipt_safe(item)
    elif isinstance(value, str) and "://" in value:
        raise ReceiptError("receipt contains a URL value")


def _receipt_mode(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiptError("receipt cannot be read") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ReceiptError("receipt must be a regular file with mode 0600")


def _write_temp_json(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


class ReceiptStore:
    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = Path(f"{self.path}.lock")
        self._lock_fd: Optional[int] = None

    def acquire(self) -> None:
        """Hold a stable sidecar lock for the complete upload/resume run."""
        if self._lock_fd is not None:
            return
        if fcntl is None:
            raise ReceiptError("receipt locking is unavailable on this platform")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            raise ReceiptError("receipt is already in use by another operation") from exc
        except OSError as exc:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            raise ReceiptError("cannot lock receipt") from exc
        self._lock_fd = fd

    def release(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
        finally:
            os.close(fd)

    def create(self, value: Mapping[str, Any]) -> None:
        if self.path.exists() or self.path.is_symlink():
            raise ReceiptError("receipt already exists; use resume explicitly")
        _assert_receipt_safe(value)
        temporary = _write_temp_json(self.path, value)
        try:
            # Hard-linking the completed temporary file makes creation fail
            # atomically if another process created the receipt meanwhile.
            os.link(temporary, self.path)
        except FileExistsError as exc:
            raise ReceiptError("receipt already exists; use resume explicitly") from exc
        finally:
            temporary.unlink(missing_ok=True)
        _receipt_mode(self.path)

    def read(self) -> Dict[str, Any]:
        _receipt_mode(self.path)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptError("receipt is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ReceiptError("receipt must contain a JSON object")
        _assert_receipt_safe(value)
        return value

    def update(self, value: Mapping[str, Any]) -> None:
        if not self.path.exists():
            raise ReceiptError("receipt does not exist")
        _receipt_mode(self.path)
        _assert_receipt_safe(value)
        temporary = _write_temp_json(self.path, value)
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        _receipt_mode(self.path)


def _new_receipt(info: PackageInfo, app_id: str, bundle_name: str, channel: str, mainland: Optional[int]) -> Dict[str, Any]:
    return {
        "schema": 1,
        "state": "prepared",
        "fileName": info.file_name,
        "size": info.size,
        "sha256": info.sha256,
        "bundleName": bundle_name,
        "versionName": info.version_name,
        "buildVersion": info.build_version,
        "metadataRelease": info.metadata_release,
        "appId": app_id,
        "channel": channel,
        "mainland": mainland,
        "objectId": None,
        "packageId": None,
        "testVersion": None,
        "uploadState": "not_started",
        "registrationState": "skipped" if channel == "testing" else "not_started",
        "testingState": "skipped" if channel == "release" else "not_started",
        "compileState": "unknown",
        "lastError": None,
    }


def _validate_receipt(value: Mapping[str, Any]) -> None:
    if value.get("schema") != 1:
        raise ReceiptError("unsupported receipt schema")
    for key in ("sha256", "appId", "channel", "uploadState", "registrationState"):
        if key not in value:
            raise ReceiptError(f"receipt is missing {key}")
    if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ReceiptError("receipt has an invalid SHA-256")
    if value.get("channel") not in {"release", "testing"}:
        raise ReceiptError("receipt has an invalid channel")
    if not isinstance(value.get("appId"), str) or not value["appId"]:
        raise ReceiptError("receipt has an invalid appId")
    _assert_receipt_safe(value)


def _safe_error(exc: BaseException) -> Dict[str, Any]:
    if isinstance(exc, HttpResponseError):
        return {"kind": "http", "status": exc.status}
    if isinstance(exc, ApiCodeError):
        return {"kind": "api", "code": _safe_scalar(exc.code)}
    if isinstance(exc, PollFailure):
        return {"kind": "compile_failed", "state": _safe_scalar(exc.state)}
    if isinstance(exc, PollTimeout):
        return {"kind": "compile_pending"}
    if isinstance(exc, NetworkError):
        return {"kind": "network"}
    if isinstance(exc, ProtocolError):
        return {"kind": "protocol"}
    if isinstance(exc, PackageError):
        return {"kind": "package"}
    return {"kind": type(exc).__name__}


def _mark(store: Optional[ReceiptStore], current: Dict[str, Any], **changes: Any) -> None:
    current.update(changes)
    if store is not None:
        store.update(current)


def _mutation_failed(
    store: Optional[ReceiptStore],
    state: Dict[str, Any],
    field: str,
    exc: BaseException,
    *,
    uncertain: bool,
) -> None:
    state[field] = "uncertain" if uncertain else "failed"
    state["lastError"] = _safe_error(exc)
    if store is not None:
        store.update(state)


def _mutation_uncertain(exc: BaseException) -> bool:
    """Classify an in-flight mutation conservatively.

    Only an explicit AGC rejection is safe to repeat.  A cancellation,
    malformed success response, transport error, or unexpected exception can
    happen after the server accepted the request, so its receipt must stop
    resume rather than silently binding the object a second time.
    """
    if isinstance(exc, CredentialsError):
        return False
    if isinstance(exc, ApiCodeError):
        return False
    if isinstance(exc, HttpResponseError):
        return exc.status >= 500
    return True


def poll_compile(
    client: AgcClient,
    app_id: str,
    package_id: str,
    *,
    timeout: float = DEFAULT_POLL_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_state: Optional[Callable[[str], None]] = None,
) -> str:
    if timeout < 0 or interval < 0:
        raise AgcError("poll timeout and interval must be non-negative")
    deadline = monotonic() + timeout
    while True:
        state = client.compile_status(app_id, package_id)
        if state == 0:
            if on_state:
                on_state("ready")
            return "ready"
        if state == 2:
            if on_state:
                on_state("failed")
            raise PollFailure(package_id, state)
        if on_state:
            on_state("pending")
        if monotonic() >= deadline:
            raise PollTimeout(package_id)
        remaining = max(0.0, deadline - monotonic())
        sleep(min(interval, remaining))


def _credentials_for_network(path: Optional[os.PathLike[str] | str]) -> Mapping[str, Any]:
    resolved = resolve_credentials_path(path)
    if resolved is None:
        raise CredentialsError("credentials are required for network operations")
    return load_credentials(resolved)


def default_receipt_path(info: PackageInfo, app_id: str, channel: str) -> Path:
    """Stable private receipt location used when the CLI flag is omitted."""
    app_digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:16]
    return DEFAULT_RECEIPT_DIR / f"{info.sha256}-{channel}-{app_digest}.json"


def _result_from_receipt(
    state: Mapping[str, Any], receipt_path: Optional[os.PathLike[str] | str] = None
) -> Dict[str, Any]:
    result = {
        "appId": state.get("appId"),
        "channel": state.get("channel"),
        "sha256": state.get("sha256"),
        "objectId": state.get("objectId"),
        "packageId": state.get("packageId"),
        "compileState": state.get("compileState"),
    }
    if state.get("channel") == "testing":
        result["testVersion"] = state.get("testVersion")
    if receipt_path is not None:
        result["receiptPath"] = str(Path(receipt_path).expanduser())
    return result


def upload_package(
    package: os.PathLike[str] | str,
    *,
    app_id: str,
    bundle_name: str,
    channel: str = "release",
    mainland: Optional[int] = None,
    credentials_path: Optional[os.PathLike[str] | str] = None,
    receipt_path: Optional[os.PathLike[str] | str] = None,
    dry_run: bool = False,
    transport: Optional[Any] = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    verify_app_id: bool = True,
) -> Dict[str, Any]:
    if channel not in {"release", "testing"}:
        raise AgcError("channel must be release or testing")
    if mainland not in (None, 0, 1):
        raise AgcError("mainland must be 0 or 1")
    if not app_id or not bundle_name:
        raise AgcError("app-id and bundle-name are required")
    info = inspect_package(package)
    if info.bundle_name != bundle_name:
        raise PackageError(
            f"bundleName mismatch: package has {info.bundle_name}, requested {bundle_name}"
        )
    if dry_run:
        return {
            "action": "dry-run",
            "appId": app_id,
            "channel": channel,
            "mainland": mainland,
            **package_info_json(info),
        }
    if receipt_path is None:
        receipt_path = default_receipt_path(info, app_id, channel)
    store = ReceiptStore(receipt_path)
    state = _new_receipt(info, app_id, bundle_name, channel, mainland)
    if store is not None:
        store.acquire()
    created = False
    try:
        credentials = _credentials_for_network(credentials_path)
        client = AgcClient(credentials, transport=transport)
        if store is not None:
            store.create(state)
            created = True
        # Resolve the developer's target identity immediately before obtaining
        # an upload URL.  An explicit app-id is accepted only when AGC returns
        # exactly one matching HarmonyOS application record.
        if verify_app_id:
            matches = client.lookup_app_ids(bundle_name)
            exact = [item for item in matches if item.get("appId") == app_id]
            if len(exact) != 1:
                raise ProtocolError("app-id lookup did not produce exactly one matching application")
        _ensure_unchanged(info)
        object_id, upload_url, upload_headers = client.get_upload_url(
            app_id, info.file_name, info.size, info.sha256, mainland
        )
        _mark(store, state, objectId=object_id, uploadState="url_obtained")
        _ensure_unchanged(info)
        _mark(store, state, uploadState="started")
        try:
            client.upload_file(info, upload_url, upload_headers)
        except BaseException as exc:
            _mutation_failed(
                store,
                state,
                "uploadState",
                exc,
                uncertain=_mutation_uncertain(exc),
            )
            raise
        try:
            _ensure_unchanged(info)
        except BaseException as exc:
            _mutation_failed(store, state, "uploadState", exc, uncertain=True)
            raise
        _mark(store, state, uploadState="uploaded")

        if channel == "release":
            _mark(store, state, registrationState="started")
            try:
                package_id = client.register_package(app_id, info.file_name, object_id)
            except BaseException as exc:
                _mutation_failed(
                    store,
                    state,
                    "registrationState",
                    exc,
                    uncertain=_mutation_uncertain(exc),
                )
                raise
            _mark(store, state, registrationState="registered", packageId=package_id)
        else:
            _mark(store, state, testingState="started")
            try:
                package_id = client.register_testing(app_id, info.file_name, object_id)
            except BaseException as exc:
                _mutation_failed(
                    store,
                    state,
                    "testingState",
                    exc,
                    uncertain=_mutation_uncertain(exc),
                )
                raise
            _mark(store, state, testingState="registered", testVersion=package_id, packageId=package_id)

        def update_state(status: str) -> None:
            _mark(store, state, compileState=status)

        try:
            poll_compile(
                client,
                app_id,
                package_id,
                timeout=poll_timeout,
                interval=poll_interval,
                sleep=sleep,
                monotonic=monotonic,
                on_state=update_state,
            )
        except BaseException as exc:
            state["lastError"] = _safe_error(exc)
            if store is not None:
                store.update(state)
            raise
        _mark(store, state, state="complete", compileState="ready", lastError=None)
        return _result_from_receipt(state, store.path if store is not None else None)
    except BaseException:
        if store is not None and created:
            # Preserve the receipt for resume or manual diagnosis.  Never turn
            # an uncertain mutation into an automatic retry.
            try:
                store.update(state)
            except ReceiptError:
                pass
        raise
    finally:
        if store is not None:
            store.release()


def _resume_receipt_locked(
    receipt_path: os.PathLike[str] | str,
    *,
    credentials_path: Optional[os.PathLike[str] | str] = None,
    transport: Optional[Any] = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    _store: Optional[ReceiptStore] = None,
) -> Dict[str, Any]:
    store = _store or ReceiptStore(receipt_path)
    state = store.read()
    _validate_receipt(state)
    for field in ("uploadState", "registrationState", "testingState"):
        if state.get(field) in {"started", "uncertain"}:
            raise UnsafeResumeError(
                f"receipt has uncertain {field}; refusing to repeat the mutation"
            )
    if state.get("uploadState") != "uploaded" or not isinstance(state.get("objectId"), str):
        raise UnsafeResumeError("resume requires a known uploaded objectId")
    credentials = _credentials_for_network(credentials_path)
    client = AgcClient(credentials, transport=transport)
    app_id = str(state["appId"])
    object_id = str(state["objectId"])
    file_name = str(state.get("fileName") or "package.app")
    if state.get("channel") == "release" and state.get("registrationState") != "registered":
        _mark(store, state, registrationState="started")
        try:
            package_id = client.register_package(app_id, file_name, object_id)
        except BaseException as exc:
            _mutation_failed(
                store,
                state,
                "registrationState",
                exc,
                uncertain=_mutation_uncertain(exc),
            )
            raise
        _mark(store, state, registrationState="registered", packageId=package_id)
    if state.get("channel") == "testing" and state.get("testingState") != "registered":
        _mark(store, state, testingState="started")
        try:
            package_id = client.register_testing(app_id, file_name, object_id)
        except BaseException as exc:
            _mutation_failed(
                store,
                state,
                "testingState",
                exc,
                uncertain=_mutation_uncertain(exc),
            )
            raise
        _mark(store, state, testingState="registered", testVersion=package_id, packageId=package_id)

    package_id = state.get("packageId")
    if not isinstance(package_id, str) or not package_id:
        raise ProtocolError("receipt has no packageId")

    def update_state(status: str) -> None:
        _mark(store, state, compileState=status)

    try:
        poll_compile(
            client,
            app_id,
            package_id,
            timeout=poll_timeout,
            interval=poll_interval,
            sleep=sleep,
            monotonic=monotonic,
            on_state=update_state,
        )
    except BaseException as exc:
        state["lastError"] = _safe_error(exc)
        store.update(state)
        raise
    _mark(store, state, state="complete", compileState="ready", lastError=None)
    return _result_from_receipt(state, store.path)


def resume_receipt(
    receipt_path: os.PathLike[str] | str,
    *,
    credentials_path: Optional[os.PathLike[str] | str] = None,
    transport: Optional[Any] = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    store = ReceiptStore(receipt_path)
    store.acquire()
    try:
        return _resume_receipt_locked(
            receipt_path,
            credentials_path=credentials_path,
            transport=transport,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            sleep=sleep,
            monotonic=monotonic,
            _store=store,
        )
    finally:
        store.release()


def status_package(
    app_id: str,
    package_id: str,
    *,
    credentials_path: Optional[os.PathLike[str] | str] = None,
    transport: Optional[Any] = None,
    wait: bool = False,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    credentials = _credentials_for_network(credentials_path)
    client = AgcClient(credentials, transport=transport)
    if not wait:
        state = client.compile_status(app_id, package_id)
        if state == 2:
            raise PollFailure(package_id, state)
        names = {0: "ready", 1: "pending", 2: "failed"}
        return {"appId": app_id, "packageId": package_id, "compileState": names[state]}
    status = poll_compile(
        client,
        app_id,
        package_id,
        timeout=poll_timeout,
        interval=poll_interval,
        sleep=sleep,
        monotonic=monotonic,
    )
    return {"appId": app_id, "packageId": package_id, "compileState": status}


def discover_app_ids(
    bundle_name: str,
    *,
    credentials_path: Optional[os.PathLike[str] | str] = None,
    transport: Optional[Any] = None,
) -> Dict[str, Any]:
    credentials = _credentials_for_network(credentials_path)
    client = AgcClient(credentials, transport=transport)
    return {"bundleName": bundle_name, "appIds": list(client.lookup_app_ids(bundle_name))}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and upload HarmonyOS .app packages to AGC")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="inspect a package offline")
    inspect_parser.add_argument("package")

    upload_parser = commands.add_parser("upload", help="upload and register a package")
    upload_parser.add_argument("package")
    upload_parser.add_argument("--app-id", required=True)
    upload_parser.add_argument("--bundle-name", required=True)
    upload_parser.add_argument("--channel", choices=("release", "testing"), default="release")
    upload_parser.add_argument("--mainland", choices=(0, 1), type=int)
    upload_parser.add_argument("--credentials", metavar="PATH")
    upload_parser.add_argument("--receipt", metavar="PATH")
    upload_parser.add_argument("--dry-run", action="store_true")
    upload_parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_TIMEOUT)
    upload_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)

    resume_parser = commands.add_parser("resume", help="continue a receipt with known safe state")
    resume_parser.add_argument("--receipt", required=True, metavar="PATH")
    resume_parser.add_argument("--credentials", metavar="PATH")
    resume_parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_TIMEOUT)
    resume_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)

    status_parser = commands.add_parser("status", help="read compile status")
    status_parser.add_argument("--app-id", required=True)
    status_parser.add_argument("--package-id", required=True)
    status_parser.add_argument("--credentials", metavar="PATH")
    status_parser.add_argument("--wait", "--poll", action="store_true", dest="wait")
    status_parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_TIMEOUT)
    status_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)

    discover_parser = commands.add_parser("discover", help="look up HarmonyOS app IDs by bundle name")
    discover_parser.add_argument("--bundle-name", required=True)
    discover_parser.add_argument("--credentials", metavar="PATH")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = package_info_json(inspect_package(args.package))
        elif args.command == "upload":
            result = upload_package(
                args.package,
                app_id=args.app_id,
                bundle_name=args.bundle_name,
                channel=args.channel,
                mainland=args.mainland,
                credentials_path=args.credentials,
                receipt_path=args.receipt,
                dry_run=args.dry_run,
                poll_timeout=args.poll_timeout,
                poll_interval=args.poll_interval,
            )
        elif args.command == "resume":
            result = resume_receipt(
                args.receipt,
                credentials_path=args.credentials,
                poll_timeout=args.poll_timeout,
                poll_interval=args.poll_interval,
            )
        elif args.command == "status":
            result = status_package(
                args.app_id,
                args.package_id,
                credentials_path=args.credentials,
                wait=args.wait,
                poll_timeout=args.poll_timeout,
                poll_interval=args.poll_interval,
            )
        elif args.command == "discover":
            result = discover_app_ids(args.bundle_name, credentials_path=args.credentials)
        else:  # pragma: no cover - argparse enforces commands.
            parser.error("unknown command")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        message = "error: interrupted"
        if args.command == "upload" and not args.dry_run:
            try:
                package_info = inspect_package(args.package)
                receipt = (
                    Path(args.receipt).expanduser()
                    if args.receipt
                    else default_receipt_path(package_info, args.app_id, args.channel)
                )
                message += f"; receipt: {receipt}"
            except (AgcError, OSError):
                pass
        elif args.command == "resume":
            message += f"; receipt: {Path(args.receipt).expanduser()}"
        print(message, file=sys.stderr)
        return 130
    except (AgcError, OSError) as exc:
        message = f"error: {exc}"
        if args.command == "upload" and not args.dry_run:
            try:
                package_info = inspect_package(args.package)
                receipt = (
                    Path(args.receipt).expanduser()
                    if args.receipt
                    else default_receipt_path(package_info, args.app_id, args.channel)
                )
                message += f"; receipt: {receipt}"
            except (AgcError, OSError):
                pass
        elif args.command == "resume":
            message += f"; receipt: {Path(args.receipt).expanduser()}"
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
