"""Exercise the 32-bit guest / native DTLS boundary on the Apple host."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'Apple host ABI')
class DTLSBridgeTests(unittest.TestCase):
    def test_guest_buffers_handles_clock_and_results_cross_the_native_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            out = pathlib.Path(folder)
            (out / 'xg_engine_compat.h').write_text(
                '#define XG_ENGINE_DTLS 1\n#define XG_UPNP_PREFERRED_PORT 1\n')
            harness = out / 'test.c'
            harness.write_text(r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "xg_posix.c"
uintptr_t xg_base;
__thread int xg_errno;
int xg_linux_errno(int value) { return value; }
static unsigned char memory[512];
static int calls;
int posix_dtls_fingerprint(unsigned char *p) {
    assert(p == memory + 32); memset(p, 0xab, 32); calls++; return 1;
}
int posix_dtls_open(void) { calls++; return 37; }
void posix_dtls_close(int h) { assert(h == 37); calls++; }
void posix_dtls_input(int h, const void *p, int n) {
    assert(h == 37 && p == memory + 64 && n == 13); calls++;
}
int posix_dtls_receive(int h, posix_ulong now, void *p, int n) {
    assert(h == 37 && now == 0xf1234567u && p == memory + 96 && n == 16);
    memcpy(p, "received", 8); calls++; return 8;
}
int posix_dtls_send(int h, const void *p, int n) {
    assert(h == -1 && p == NULL && n == 0); calls++; return -1;
}
int posix_dtls_output(int h, void *p, int n) {
    assert(h == 37 && p == memory + 128 && n == 20);
    memcpy(p, "output", 6); calls++; return 6;
}
int posix_dtls_peer_fingerprint(int h, unsigned char *p) {
    assert(h == 37 && p == memory + 160); memset(p, 0xcd, 32); calls++; return 1;
}
void posix_hmac_sha1(const void *key, int kn, const void *data, int dn, unsigned char *p) {
    assert(key == memory + 208 && kn == 3 && data == memory + 224 && dn == 5);
    assert(p == memory + 256); memset(p, 0xef, 20); calls++;
}
int main(void) {
    xg_base = (uintptr_t)memory;
    assert(xh_hostposix_dtls_fingerprint(32) == 1 && memory[63] == 0xab);
    assert(xh_hostposix_dtls_open() == 37);
    xh_hostposix_dtls_close(37);
    xh_hostposix_dtls_input(37, 64, 13);
    assert(xh_hostposix_dtls_receive(37, 0xf1234567u, 96, 16) == 8);
    assert(!memcmp(memory + 96, "received", 8));
    assert(xh_hostposix_dtls_send(-1, 0, 0) == -1);
    assert(xh_hostposix_dtls_output(37, 128, 20) == 6);
    assert(!memcmp(memory + 128, "output", 6));
    assert(xh_hostposix_dtls_peer_fingerprint(37, 160) == 1 && memory[191] == 0xcd);
    xh_hostposix_hmac_sha1(208, 3, 224, 5, 256);
    assert(memory[275] == 0xef && memory[276] == 0 && calls == 9);
    return 0;
}
''')
            binary = out / 'bridge'
            subprocess.run(['clang', '-Wall', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(ROOT / 'port/xbox'), '-I', folder,
                            str(harness), '-Wl,-dead_strip', '-o', str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_older_engine_build_has_no_dtls_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            out = pathlib.Path(folder)
            (out / 'xg_engine_compat.h').write_text(
                '#define XG_ENGINE_DTLS 0\n#define XG_UPNP_PREFERRED_PORT 0\n')
            obj = out / 'posix.o'
            subprocess.run(['clang', '-Wall', '-Werror', '-I', folder, '-c',
                            str(ROOT / 'port/xbox/xg_posix.c'), '-o', str(obj)],
                           check=True, capture_output=True)
            symbols = subprocess.check_output(['nm', '-u', str(obj)], text=True)
            self.assertNotIn('dtls', symbols)
            self.assertNotIn('hmac', symbols)


if __name__ == '__main__':
    unittest.main()
