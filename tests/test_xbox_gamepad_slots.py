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
#import <dispatch/dispatch.h>
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
/* Force a guest refresh to wait at its first lock, then run it between the
 * UI refresh's unlock and its primary-slot query. These wrappers schedule
 * real mutex operations; production discovery and slot logic stay intact. */
static _Thread_local int stale_worker, pause_stale_lock;
static int force_ui_unlock;
static dispatch_semaphore_t stale_at_lock, release_stale, stale_done;
static int scheduled_lock(pthread_mutex_t *lock) {
    if (stale_worker && pause_stale_lock) {
        pause_stale_lock = 0;
        dispatch_semaphore_signal(stale_at_lock);
        dispatch_semaphore_wait(release_stale, DISPATCH_TIME_FOREVER);
    }
    return pthread_mutex_lock(lock);
}
static int scheduled_unlock(pthread_mutex_t *lock) {
    int result = pthread_mutex_unlock(lock);
    if (!stale_worker && force_ui_unlock) {
        force_ui_unlock = 0;
        dispatch_semaphore_signal(release_stale);
        dispatch_semaphore_wait(stale_done, DISPATCH_TIME_FOREVER);
    }
    return result;
}
#define pthread_mutex_lock scheduled_lock
#define pthread_mutex_unlock scheduled_unlock
''' + declarations + '\n' + functions + '\nstatic BOOL overlay_hides_touch(void) {' + callback + r'''
}
static void *guest_poll(void *unused) {
    (void)unused;
    @autoreleasepool {
        stale_worker = pause_stale_lock = 1;
        pads_refresh();
        dispatch_semaphore_signal(stale_done);
    }
    return NULL;
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

        /* A poll that began before disconnect must not restore its stale
         * primary after the UI refreshed, or touch stays hidden without
         * another controller notification. Player two still owns slot two. */
        connected = @[a, b];
        assert(xh_host_sdl_get_gamepads(16, 8) == 2);
        stale_at_lock = dispatch_semaphore_create(0);
        release_stale = dispatch_semaphore_create(0);
        stale_done = dispatch_semaphore_create(0);
        pthread_t poll;
        assert(pthread_create(&poll, NULL, guest_poll, NULL) == 0);
        dispatch_semaphore_wait(stale_at_lock, DISPATCH_TIME_FOREVER);
        connected = @[b];
        force_ui_unlock = 1;
        assert(!overlay_hides_touch());
        assert(pthread_join(poll, NULL) == 0);
        assert(pads[1] == nil && pads[2] == b);
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
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
