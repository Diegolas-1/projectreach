"""Exercise production GameController slot/visibility code at its native API boundary."""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def function(source, name):
    match = re.search(r'^(?:static )?[^\n;{}]*\b' + re.escape(name) + r'\([^;{}]*\)\s*\{', source, re.M)
    if not match:
        raise AssertionError(f'missing production function {name}')
    opening = source.index('{', match.start())
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


class GamepadSlotsTests(unittest.TestCase):
    def test_primary_disconnect_restores_touch_without_moving_second_player(self):
        native = (ROOT / 'port/xbox/xg_ios.m').read_text()
        view = (ROOT / 'port/ios/HaloPadXbox.m').read_text()
        declarations = native[native.index('#define PADS '):native.index('static struct xg_touch_input')]
        callback = re.search(r'pad\.controllerConnected = \^BOOL \{(.*?)\n\t\t\};', view, re.S).group(1)
        names = ['pads_refresh', 'xg_ios_controller_connected', 'pad_connected',
                 'xh_host_sdl_get_gamepads', 'xh_host_sdl_open_gamepad']
        if 'static GCController *hardware_pad(' in native:
            names.insert(1, 'hardware_pad')
        functions = '\n'.join(function(native, name) for name in names)
        source = r'''
#import <Foundation/Foundation.h>
#include <pthread.h>
#include <stdint.h>
#include <assert.h>
/* Only the platform discovery API is fake. Slot allocation/query and the
 * actual production overlay's visibility callback below are unmodified. */
@interface GCController : NSObject
@property(nonatomic, strong) id extendedGamepad;
+ (NSArray<GCController *> *)controllers;
@end
static NSArray<GCController *> *connected;
@implementation GCController
+ (NSArray<GCController *> *)controllers { return connected; }
@end
static uint8_t guest[256];
#define G(type, address) ((type)(guest + (address)))
''' + declarations + '\n' + functions + '\nstatic BOOL overlay_hides_touch(void) {' + callback + r'''
}
int main(void) {
    @autoreleasepool {
        unsetenv("XG_TOUCH_SHOW");
        GCController *a = [GCController new], *b = [GCController new], *c = [GCController new];
        GCController *unsupported = [GCController new];
        a.extendedGamepad = b.extendedGamepad = c.extendedGamepad = [NSObject new];
        connected = @[a, b];
        assert(xh_host_sdl_get_gamepads(16, 8) == 2);
        assert(G(uint32_t *,16)[0] == 1 && G(uint32_t *,16)[1] == 2);
        assert(overlay_hides_touch());
        /* Real notification order: discovery changes before the game polls.
         * Player two must remain player two; the touch pad now owns player one. */
        connected = @[b];
        assert(!overlay_hides_touch());
        assert(pads[1] == nil && pads[2] == b);
        assert(xh_host_sdl_get_gamepads(16, 8) == 2);
        assert(G(uint32_t *,16)[0] == 1 && G(uint32_t *,16)[1] == 2);
        assert(xh_host_sdl_open_gamepad(1) == 1 && xh_host_sdl_open_gamepad(2) == 2);
        /* A new primary can take the vacant slot without stealing player two. */
        connected = @[b, c];
        assert(overlay_hides_touch());
        assert(pads[1] == c && pads[2] == b);
        setenv("XG_TOUCH_SHOW", "1", 1);
        assert(!overlay_hides_touch());
        unsetenv("XG_TOUCH_SHOW");
        connected = @[c];
        assert(overlay_hides_touch());
        assert(xh_host_sdl_get_gamepads(16, 8) == 1);
        assert(xh_host_sdl_open_gamepad(2) == 0);
        connected = @[unsupported];
        assert(!overlay_hides_touch());
        assert(xh_host_sdl_get_gamepads(16, 8) == 1); /* permanent touch slot */
        assert(xh_host_sdl_open_gamepad(0) == 0 && xh_host_sdl_open_gamepad(9) == 0);
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = pathlib.Path(tmp) / 'slots'
            built = subprocess.run(['clang', '-x', 'objective-c', '-', '-fobjc-arc',
                                    '-Wall', '-Wextra', '-Werror', '-framework', 'Foundation',
                                    '-pthread', '-o', str(binary)], input=source, text=True,
                                   capture_output=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
