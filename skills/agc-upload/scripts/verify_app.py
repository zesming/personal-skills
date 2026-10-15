#!/usr/bin/env python3
"""Verify the signed App Pack with the installed SDK, then check release identity."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from agc_upload import AgcError, inspect_package


def find_tools():
    studio = Path(os.environ.get('DEVECO_PATH', '/Applications/DevEco-Studio.app'))
    if studio.suffix == '.app':
        studio = studio / 'Contents'
    jar = studio / 'sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar'
    candidates = [studio / 'jbr/Contents/Home/bin/java', studio / 'jbr/bin/java']
    java = next((str(p) for p in candidates if p.is_file()), shutil.which('java'))
    if not jar.is_file() or not java:
        raise AgcError('SDK signing verifier unavailable; set DEVECO_PATH to DevEco Studio')
    return java, jar


def verify(package, expected_bundle):
    info = inspect_package(package)
    if info.bundle_name != expected_bundle:
        raise AgcError('Package bundle does not match requested signing identity')
    java, jar = find_tools()
    results = []
    with tempfile.TemporaryDirectory(prefix='agc-verify-') as temporary:
        root = Path(temporary)
        cert = root / 'chain.cer'
        profile = root / 'profile.p7b'
        # Current DevEco signs the complete App Pack. The inner HAP files may
        # be unsigned; the outer signature covers their bytes and pack metadata.
        result = subprocess.run([java, '-jar', str(jar), 'verify-app', '-inFile', str(Path(package).resolve()),
                                 '-outCertChain', str(cert), '-outProfile', str(profile)],
                                capture_output=True)
        if result.returncode != 0 or not cert.is_file() or not profile.is_file():
            raise AgcError('SDK rejected the App Pack signature; diagnostic output suppressed')
        payload = subprocess.run(['openssl', 'cms', '-verify', '-noverify', '-inform', 'DER',
                                  '-in', str(profile)], capture_output=True)
        if payload.returncode:
            raise AgcError('Cannot verify and decode embedded Profile')
        data = json.loads(payload.stdout)
        if data.get('type') != 'release':
            raise AgcError('Embedded Profile is not a release Profile')
        if data.get('bundle-info', {}).get('bundle-name') != expected_bundle:
            raise AgcError('Embedded Profile bundle differs from package')
        distribution = data.get('app-distribution-type')
        if distribution != 'app_gallery':
            raise AgcError('Embedded Profile is not for AppGallery distribution')
        results.append({'package': Path(package).name, 'signature': 'verified', 'profileType': 'release',
                        'distributionType': distribution})
    digest = hashlib.sha256()
    with Path(package).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != info.sha256:
        raise AgcError('App Pack changed during signature verification')
    return {'signatureVerified': True, 'bundleName': expected_bundle, 'sha256': info.sha256,
            'versionName': info.version_name, 'buildVersion': info.build_version, 'signedArtifacts': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--bundle-name', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.package, args.bundle_name)))
        return 0
    except (AgcError, OSError, ValueError, KeyError) as error:
        # Third-party tools and parsers may include raw signing contents in errors.
        print(json.dumps({'error': 'Signature verification failed', 'type': type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
