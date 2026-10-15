#!/usr/bin/env python3
"""Reserve a numeric HarmonyOS buildVersion and build a whole product."""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

import json5


_BUILD_MODE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')


def _read_profile(profile, *, require_signing_configs=False):
    try:
        config = json5.loads(profile.read_text(encoding='utf-8'))
    except (OSError, TypeError, ValueError):
        raise ValueError('Invalid build profile') from None
    if not isinstance(config, dict) or not isinstance(config.get('app'), dict):
        raise ValueError('Invalid build profile')
    app = config['app']
    if not isinstance(app.get('products'), list):
        raise ValueError('Invalid build profile')
    if require_signing_configs and (
        not isinstance(app.get('signingConfigs'), list) or not app['signingConfigs']
    ):
        raise ValueError('Runtime build profile must contain signingConfigs')
    return config


def _matching_product(config, product_name):
    products = [
        product for product in config['app']['products']
        if isinstance(product, dict) and product.get('name') == product_name
    ]
    if len(products) != 1:
        raise ValueError('Expected exactly one matching product')
    return products[0]


def _validate_build_mode(build_mode):
    if not isinstance(build_mode, str) or not _BUILD_MODE_RE.fullmatch(build_mode):
        raise ValueError('Invalid build mode')
    return build_mode


def _validate_runtime_profile(project, runtime_profile, product_name):
    if runtime_profile is None:
        return None, None
    try:
        profile = Path(runtime_profile).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        raise ValueError('Runtime build profile must be an existing file with mode 0600') from None
    if not profile.is_file():
        raise ValueError('Runtime build profile must be an existing file with mode 0600')
    try:
        profile.relative_to(project)
    except ValueError:
        pass
    else:
        raise ValueError('Runtime build profile must be outside the project')
    try:
        mode = stat.S_IMODE(profile.stat().st_mode)
    except OSError:
        raise ValueError('Runtime build profile must be an existing file with mode 0600') from None
    if mode != 0o600:
        raise ValueError('Runtime build profile must have mode 0600')
    config = _read_profile(profile, require_signing_configs=True)
    product = _matching_product(config, product_name)
    return profile, product


def reserve_version(profile, product_name, version_name, requested):
    config = _read_profile(profile)
    product = _matching_product(config, product_name)
    current = str(product.get('buildVersion', '0'))
    if requested is None:
        if not current.isascii() or not current.isdigit():
            raise ValueError('Existing buildVersion is not numeric; specify --build-number')
        number = str(int(current) + 1)
    else:
        if not isinstance(requested, str):
            raise ValueError('Build number must be 1–18 digits with no leading zero')
        number = requested
    if not number.isascii() or not number.isdigit() or len(number) > 18 or str(int(number)) != number:
        raise ValueError('Build number must be 1–18 digits with no leading zero')
    if not isinstance(version_name, str) or not version_name or len(version_name) > 127:
        raise ValueError('Invalid version name')
    product['versionName'] = version_name
    product['buildVersion'] = number
    # This file includes signing material: never print it or put a backup in the repo.
    fd, temporary = tempfile.mkstemp(prefix='.agc-build-', dir=profile.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, profile)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return number


def build(project, product, version, number, output_dir, build_mode='release', runtime_profile=None):
    project = project.resolve()
    profile = project / 'build-profile.json5'
    if not profile.is_file():
        raise ValueError('Configure the local build-profile.json5 and signing first')
    build_mode = _validate_build_mode(build_mode)
    project_config = _read_profile(profile)
    project_product = _matching_product(project_config, product)
    runtime_path, runtime_product = _validate_runtime_profile(project, runtime_profile, product)
    if not shutil.which('devecocli'):
        raise ValueError('devecocli is not installed')
    if version is None:
        selected = runtime_product if runtime_product is not None else project_product
        version = selected.get('versionName')
        if version is None and runtime_product is not None:
            version = project_product.get('versionName')
        if version is None:
            version = json5.loads((project / 'AppScope/app.json5').read_text())['app']['versionName']
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    lock_dir = Path.home() / '.cache/agc-upload/locks'
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / (hashlib.sha256(str(project).encode()).hexdigest() + '.lock')
    with open(lock_path, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another AGC build is running for this project') from None
        number = reserve_version(runtime_path or profile, product, version, number)
        fd, log_path = tempfile.mkstemp(prefix=f'build-{number}-', suffix='.log', dir=output_dir)
        started = time.time()
        with os.fdopen(fd, 'w') as log:
            command = ['devecocli', 'build', '--product', product, '--build-mode', build_mode]
            run_options = {'cwd': project, 'stdout': log, 'stderr': subprocess.STDOUT}
            if runtime_path is not None:
                environment = os.environ.copy()
                environment['AGC_RUNTIME_BUILD_PROFILE'] = str(runtime_path)
                run_options['env'] = environment
            elif 'AGC_RUNTIME_BUILD_PROFILE' in os.environ:
                environment = os.environ.copy()
                environment.pop('AGC_RUNTIME_BUILD_PROFILE', None)
                run_options['env'] = environment
            result = subprocess.run(command, **run_options)
        packages = sorted(p for p in (project / 'build' / 'outputs' / product).rglob('*.app')
                          if p.is_file() and p.stat().st_mtime >= started - 1)
        report = {'versionName': version, 'buildVersion': number, 'buildMode': build_mode,
                  'exitCode': result.returncode,
                  'packages': [str(p) for p in packages], 'log': log_path,
                  'signingVerified': False}
        print(json.dumps(report, ensure_ascii=False))
        return result.returncode if result.returncode else (0 if packages else 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True, help='HarmonyOS project directory')
    parser.add_argument('--product', default='default')
    parser.add_argument('--version-name', help='Omit to preserve the current product/app versionName')
    parser.add_argument('--build-number', help='Omit to increment the current numeric buildVersion')
    parser.add_argument('--build-mode', default='release', help='Build mode (default: release)')
    parser.add_argument('--runtime-profile', type=Path,
                        help='Protected build-profile outside the project for hooked runtime builds')
    parser.add_argument('--output-dir', type=Path, default=Path.home() / '.local/state/agc-upload/builds')
    args = parser.parse_args()
    try:
        return build(args.project, args.product, args.version_name, args.build_number, args.output_dir,
                     args.build_mode, args.runtime_profile)
    except (ValueError, OSError, KeyError):
        # Do not interpolate parsing/process exceptions: they may contain signing data.
        print(json.dumps({'error': 'Build preparation failed; check product, build number, configuration and active build lock.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
