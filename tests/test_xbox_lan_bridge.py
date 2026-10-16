"""Exercise real guest socket wrappers, including Darwin sockaddr conversion."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'Apple host ABI')
class LANBridgeTests(unittest.TestCase):
    def test_fanout_errors_manual_addresses_and_interface_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out / 'xg_engine_compat.h').write_text(
                '#define XG_ENGINE_DTLS 0\n#define XG_UPNP_PREFERRED_PORT 0\n')
            binary = out / 'lan-bridge'
            subprocess.run([
                'clang', '-Wall', '-Wextra', '-Werror', '-DXG_BONJOUR=1',
                '-fsanitize=address,undefined', '-I', str(ROOT / 'port/xbox'),
                '-I', folder, str(ROOT / 'tests/xbox_lan_bridge_test.c'),
                '-Wl,-dead_strip', '-o', str(binary),
            ], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_native_sdl_bridge_has_no_bonjour_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out / 'xg_engine_compat.h').write_text(
                '#define XG_ENGINE_DTLS 0\n#define XG_UPNP_PREFERRED_PORT 0\n')
            obj = out / 'posix.o'
            subprocess.run([
                'clang', '-Wall', '-Werror', '-I', folder, '-c',
                str(ROOT / 'port/xbox/xg_posix.c'), '-o', str(obj),
            ], check=True, capture_output=True)
            symbols = subprocess.check_output(['nm', '-u', str(obj)], text=True)
            self.assertNotIn('xg_lan_', symbols)


if __name__ == '__main__':
    unittest.main()
