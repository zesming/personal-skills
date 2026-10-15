import json
import io
import inspect
import os
from contextlib import redirect_stdout
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from build_app import build, reserve_version


class BuildVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / 'build-profile.json5'
        self.original = {'app': {'products': [{'name': 'default', 'versionCode': 1}],
                                 'signingConfigs': [{'material': {'storePassword': 'test-only'}}]}}
        self.profile.write_text(json.dumps(self.original))

    def test_auto_increment_preserves_version_code_and_signing(self):
        self.assertEqual(reserve_version(self.profile, 'default', '1.0.0', None), '1')
        self.assertEqual(reserve_version(self.profile, 'default', '1.0.0', None), '2')
        config = json.loads(self.profile.read_text())
        self.assertEqual(config['app']['products'][0]['versionCode'], 1)
        self.assertEqual(config['app']['products'][0]['versionName'], '1.0.0')
        self.assertEqual(config['app']['signingConfigs'], self.original['app']['signingConfigs'])
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)

    def test_explicit_number_can_retry_same_reserved_build(self):
        self.assertEqual(reserve_version(self.profile, 'default', '1.0.0', '9'), '9')
        self.assertEqual(reserve_version(self.profile, 'default', '1.0.0', '9'), '9')
        self.assertEqual(reserve_version(self.profile, 'default', '1.0.0', None), '10')

    def test_invalid_input_does_not_modify_signing_file(self):
        before = self.profile.read_bytes()
        for number in ('01', '1.2', '1234567890123456789', '-1', '１'):
            with self.assertRaises(ValueError):
                reserve_version(self.profile, 'default', '1.0.0', number)
            self.assertEqual(self.profile.read_bytes(), before)
        with self.assertRaises(ValueError):
            reserve_version(self.profile, 'missing', '1.0.0', None)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_build_preserves_app_version_when_not_requested(self):
        project = self.profile.parent
        (project / 'AppScope').mkdir()
        (project / 'AppScope/app.json5').write_text('{app: {versionName: "2.4.0"}}')
        def fake_build(command, **kwargs):
            self.assertEqual(command, ['devecocli', 'build', '--product', 'default', '--build-mode', 'release'])
            self.assertNotIn('AGC_RUNTIME_BUILD_PROFILE', kwargs.get('env', {}))
            out = project / 'build/outputs/default'
            out.mkdir(parents=True)
            (out / 'new.app').write_bytes(b'fake build output')
            return type('Result', (), {'returncode': 0})()
        output = io.StringIO()
        with patch('build_app.shutil.which', return_value='/fake/devecocli'), patch('build_app.subprocess.run', side_effect=fake_build), redirect_stdout(output):
            self.assertEqual(build(project, 'default', None, None, project / 'logs'), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['versionName'], '2.4.0')
        self.assertEqual(result['buildVersion'], '1')
        self.assertNotIn('test-only', output.getvalue())

    def test_new_build_options_follow_legacy_parameters_with_defaults(self):
        parameters = list(inspect.signature(build).parameters.values())
        self.assertEqual([parameter.name for parameter in parameters[:5]],
                         ['project', 'product', 'version', 'number', 'output_dir'])
        self.assertEqual(parameters[5].name, 'build_mode')
        self.assertEqual(parameters[5].default, 'release')
        self.assertEqual(parameters[6].name, 'runtime_profile')
        self.assertIsNone(parameters[6].default)

    def _make_project(self):
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        (project / 'build-profile.json5').write_bytes(self.profile.read_bytes())
        (project / 'AppScope').mkdir()
        (project / 'AppScope/app.json5').write_text('{app: {versionName: "2.4.0"}}')
        return project

    def _make_runtime_profile(self, product='default', build_version='7'):
        runtime_profile = Path(self.temp.name) / 'runtime-build-profile.json5'
        runtime_profile.write_text(json.dumps({
            'app': {
                'products': [{'name': product, 'versionName': '2.5.0', 'buildVersion': build_version}],
                'signingConfigs': [{
                    'name': 'release',
                    'material': {'storePassword': 'runtime-secret'},
                }],
            },
        }))
        os.chmod(runtime_profile, 0o600)
        return runtime_profile

    def test_runtime_profile_reserves_external_and_passes_mode_env(self):
        project = self._make_project()
        runtime_profile = self._make_runtime_profile()
        project_profile = project / 'build-profile.json5'
        project_before = project_profile.read_bytes()
        captured = {}

        def fake_build(command, **kwargs):
            captured['command'] = command
            captured['env'] = kwargs['env']
            out = project / 'build/outputs/default'
            out.mkdir(parents=True)
            (out / 'candidate.app').write_bytes(b'fake build output')
            return type('Result', (), {'returncode': 0})()

        output = io.StringIO()
        with patch('build_app.shutil.which', return_value='/fake/devecocli'), \
                patch('build_app.subprocess.run', side_effect=fake_build), \
                redirect_stdout(output):
            result = build(project, 'default', '2.5.0', None, project / 'logs',
                           'releaseCandidate', runtime_profile)

        self.assertEqual(result, 0)
        self.assertEqual(captured['command'], [
            'devecocli', 'build', '--product', 'default', '--build-mode', 'releaseCandidate',
        ])
        self.assertEqual(captured['env']['AGC_RUNTIME_BUILD_PROFILE'], str(runtime_profile.resolve()))
        self.assertEqual(project_profile.read_bytes(), project_before)
        runtime = json.loads(runtime_profile.read_text())
        self.assertEqual(runtime['app']['products'][0]['buildVersion'], '8')
        self.assertEqual(runtime['app']['signingConfigs'][0]['material']['storePassword'], 'runtime-secret')
        self.assertNotIn('runtime-secret', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['buildMode'], 'releaseCandidate')

    def test_runtime_profile_inside_project_is_rejected(self):
        project = self._make_project()
        runtime_profile = project / 'runtime-build-profile.json5'
        runtime_profile.write_text(json.dumps({
            'app': {
                'products': [{'name': 'default'}],
                'signingConfigs': [{}],
            },
        }))
        os.chmod(runtime_profile, 0o600)
        with self.assertRaisesRegex(ValueError, 'outside the project'):
            build(project, 'default', '2.5.0', '1', project / 'logs', runtime_profile=runtime_profile)

    def test_missing_or_unsafe_runtime_profile_is_rejected(self):
        project = self._make_project()
        missing = Path(self.temp.name) / 'missing-runtime-profile.json5'
        with self.assertRaisesRegex(ValueError, 'existing file'):
            build(project, 'default', '2.5.0', '1', project / 'logs', runtime_profile=missing)

        unsafe = Path(self.temp.name) / 'unsafe-runtime-profile.json5'
        unsafe.write_text(json.dumps({
            'app': {
                'products': [{'name': 'default'}],
                'signingConfigs': [{}],
            },
        }))
        os.chmod(unsafe, 0o644)
        with self.assertRaisesRegex(ValueError, 'mode 0600'):
            build(project, 'default', '2.5.0', '1', project / 'logs', runtime_profile=unsafe)

    def test_runtime_product_mismatch_is_rejected(self):
        project = self._make_project()
        runtime_profile = self._make_runtime_profile(product='other')
        project_before = (project / 'build-profile.json5').read_bytes()
        with self.assertRaisesRegex(ValueError, 'matching product'):
            build(project, 'default', '2.5.0', '1', project / 'logs', runtime_profile=runtime_profile)
        self.assertEqual((project / 'build-profile.json5').read_bytes(), project_before)


if __name__ == '__main__':
    unittest.main()
