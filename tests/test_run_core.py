"""Runner failures must reach shell callers as well as the saved evidence."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'run-core.py'
spec = importlib.util.spec_from_file_location('run_core', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SDKSelectionTests(unittest.TestCase):
    def test_device_and_simulator_ignore_injected_macos_sdk(self):
        for target, sdk in (('arm64-apple-ios17.4', 'iphoneos'),
                            ('arm64-apple-ios17.4-simulator', 'iphonesimulator'),
                            ('x86_64-apple-ios17.0-simulator', 'iphonesimulator')):
            with self.subTest(target=target), \
                    patch.dict(os.environ, {'SDKROOT': '/Xcode/SDKs/MacOSX.sdk'}), \
                    patch.object(runner.subprocess, 'run', return_value=type('Result', (), {'stdout': '/correct.sdk\n'})()) as run:
                self.assertEqual(runner.sdk_path(target), '/correct.sdk')
                self.assertEqual(run.call_args.args[0], ['xcrun', '--sdk', sdk, '--show-sdk-path'])

    def test_matching_sdk_overrides_remain_supported(self):
        for target, sdk in (('arm64-apple-ios17.4', '/custom/iPhoneOS26.3.sdk'),
                            ('arm64-apple-ios17.4-simulator', '/custom/iPhoneSimulator.sdk'),
                            ('arm64-apple-ios17.4-macabi', '/custom/MacOSX27.0.sdk')):
            with self.subTest(target=target), patch.dict(os.environ, {'SDKROOT': sdk}), \
                    patch.object(runner.subprocess, 'run') as run:
                self.assertEqual(runner.sdk_path(target), sdk)
                run.assert_not_called()

    def test_catalyst_uses_macos_sdk_even_when_environment_is_ios(self):
        for override in ('', '/custom/iPhoneOS27.0.sdk'):
            with self.subTest(override=override), patch.dict(os.environ, {'SDKROOT': override}), \
                    patch.object(runner.subprocess, 'run', return_value=type('Result', (), {'stdout': '/MacOSX.sdk\n'})()) as run:
                self.assertEqual(runner.sdk_path('arm64-apple-ios17.4-macabi'), '/MacOSX.sdk')
                self.assertEqual(run.call_args.args[0], ['xcrun', '--sdk', 'macosx', '--show-sdk-path'])

    def test_desktop_preserves_default_and_explicit_sdk(self):
        for override in (None, '/custom/MacOSX.sdk'):
            with patch.dict(os.environ, {}, clear=True), patch.object(runner.subprocess, 'run') as run:
                if override:
                    os.environ['SDKROOT'] = override
                self.assertEqual(runner.sdk_path('arm64-apple-darwin'), override)
                run.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', "Apple's Python and iPhone SDK are required")
    def test_apple_python_shim_can_compile_uikit_for_iphone(self):
        # Match the failing clean PadMint path: starting /usr/bin/python3 again
        # reintroduces the Mac SDK after its parent removed SDKROOT.
        code = """
import importlib.util, pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('sdk_test', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sdk = module.sdk_path('arm64-apple-ios17.4')
assert pathlib.Path(sdk).name.lower().startswith('iphoneos'), sdk
subprocess.run(['xcrun', 'clang', '-target', 'arm64-apple-ios17.4', '-isysroot', sdk,
                '-fobjc-arc', '-x', 'objective-c', '-fsyntax-only', '-'],
               input='#import <UIKit/UIKit.h>\\n', text=True, check=True, capture_output=True)
"""
        env = {key: value for key, value in os.environ.items()
               if key not in ('SDKROOT', 'CPATH', 'LIBRARY_PATH')}
        result = subprocess.run(['/usr/bin/python3', '-c', code, str(SCRIPT)], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RunCoreExitTests(unittest.TestCase):
    def run_fixture(self, body, *args):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder).resolve()
            exe = root / 'fixture'
            exe.write_text('#!/bin/sh\n' + body + '\n')
            exe.chmod(0o700)
            argv = [str(SCRIPT), '--work', str(root), '--target', 'test', *args]
            with patch.object(runner, 'ROOT', root), \
                 patch.object(runner, 'IMAGE', exe), \
                 patch.object(runner, 'build', return_value=(exe, exe)), \
                 patch.object(runner.sys, 'argv', argv), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = runner.main()
            results = [json.loads(p.read_text()) for p in sorted(root.rglob('result.json'))]
            return code, results

    def test_success(self):
        code, results = self.run_fixture('exit 0')
        self.assertEqual(code, 0)
        self.assertEqual(results[0]['exit'], 0)

    def test_failed_client(self):
        code, results = self.run_fixture('exit 7')
        self.assertEqual(code, 7)
        self.assertEqual(results[0]['exit'], 7)

    def test_failed_relaunch_is_not_hidden_by_other_success(self):
        for failed_launch in ('1', '2'):
            with self.subTest(failed_launch=failed_launch):
                code, results = self.run_fixture(
                    f'[ "$HALOPAD_LAUNCH" = "{failed_launch}" ] && exit 3\nexit 0', '--relaunch')
                self.assertEqual(code, 3)
                self.assertEqual(len(results), 2)
                self.assertEqual(results[int(failed_launch) - 1]['exit'], 3)

    def test_trap_with_zero_exit_still_fails(self):
        code, results = self.run_fixture('echo "HALOPAD TRAP fixture" >&2\nexit 0')
        self.assertEqual(code, 1)
        self.assertEqual(results[0]['stopped_at'], 'HALOPAD TRAP fixture')

    def test_relaunch_preserves_registry_and_save_state(self):
        code, results = self.run_fixture('''
if [ "$HALOPAD_LAUNCH" = 1 ]; then
    mkdir -p "$HALOPAD_STATE_ROOT"
    echo saved-profile > "$HALOPAD_STATE_ROOT/profile"
    echo saved-setting > "$HALOPAD_REGISTRY"
else
    [ "$(cat "$HALOPAD_STATE_ROOT/profile")" = saved-profile ] || exit 5
    [ "$(cat "$HALOPAD_REGISTRY")" = saved-setting ] || exit 6
fi
''', '--relaunch')
        self.assertEqual(code, 0)
        self.assertEqual([r['exit'] for r in results], [0, 0])

    def test_timeout(self):
        # Supplying a prefix skips macOS stack sampling; exec prevents an orphan child
        # retaining the output pipes after the runner kills the timed-out process.
        code, results = self.run_fixture('exec sleep 20', '--timeout', '1', '--run-prefix', '/usr/bin/env')
        self.assertEqual(code, 124)
        self.assertEqual(results[0]['exit'], 'timeout')


if __name__ == '__main__':
    unittest.main()
