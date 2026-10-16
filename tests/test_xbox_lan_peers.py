"""Run production Bonjour address validation and peer snapshots as native C."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LANPeersTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('clang'), 'Apple Foundation required')
    def test_production_delegate_and_background_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='halopad-lan-lifecycle-') as folder:
            path = Path(folder)
            (path / 'UIKit').mkdir()
            (path / 'UIKit/UIKit.h').write_text('''#pragma once
#import <Foundation/Foundation.h>
typedef NS_ENUM(NSInteger, UIApplicationState) {
    UIApplicationStateActive, UIApplicationStateInactive, UIApplicationStateBackground
};
extern NSString *const UIApplicationDidEnterBackgroundNotification;
extern NSString *const UIApplicationDidBecomeActiveNotification;
@interface UIApplication : NSObject
@property UIApplicationState applicationState;
+ (instancetype)sharedApplication;
@end
''')
            subprocess.run(['clang', '-fobjc-arc', '-Wno-deprecated-declarations',
                            '-I', str(path), '-I', str(ROOT / 'port/xbox'),
                            str(ROOT / 'tests/xbox_lan_lifecycle_test.m'), '-framework', 'Foundation',
                            '-o', str(path / 'test')], check=True, capture_output=True)
            subprocess.run([str(path / 'test')], check=True, capture_output=True, timeout=30)

    @unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
    def test_resolver_boundaries_removal_and_concurrent_snapshots(self):
        source = r'''
#include "xg_lan_peers.h"
#include <assert.h>

static uint32_t ip(const char *text) {
    struct in_addr result;
    assert(inet_pton(AF_INET, text, &result) == 1);
    return result.s_addr;
}
static struct xg_lan_cache cache = XG_LAN_CACHE_INITIALIZER;
static void *writer(void *unused) {
    (void)unused;
    uint32_t a[] = {1, 2, 3}, b[] = {4, 5};
    for (int i = 0; i < 20000; i++) {
        xg_lan_cache_replace(&cache, a, 3);
        xg_lan_cache_replace(&cache, b, 2);
        xg_lan_cache_replace(&cache, NULL, 0);
    }
    return NULL;
}
int main(void) {
    struct xg_lan_interface nets[] = {
        {ip("192.168.1.10"), ip("255.255.255.0")},
        {ip("192.168.1.20"), ip("255.255.255.0")},
        {ip("169.254.3.7"), ip("255.255.0.0")},
        {ip("198.51.100.40"), ip("255.255.255.254")}
    };
    assert(xg_lan_on_link(ip("192.168.1.21"), nets, 4));
    assert(xg_lan_on_link(ip("169.254.8.9"), nets, 4));
    assert(xg_lan_on_link(ip("198.51.100.41"), nets, 4)); /* /31 peer */
    nets[3].address = ip("198.51.100.41");
    assert(xg_lan_on_link(ip("198.51.100.40"), nets, 4)); /* other /31 end */
    const char *reject[] = {"0.0.0.0", "127.0.0.1", "224.0.0.251", "255.255.255.255",
        "192.168.1.0", "192.168.1.255", "192.168.1.10", "192.168.1.20",
        "192.168.2.21", "8.8.8.8", "198.51.100.41"};
    for (size_t i = 0; i < sizeof(reject) / sizeof(*reject); i++)
        assert(!xg_lan_on_link(ip(reject[i]), nets, 4));
    struct xg_lan_interface invalid = {ip("192.168.1.10"), 0};
    assert(!xg_lan_on_link(ip("192.168.1.21"), &invalid, 1));
    invalid.mask = ip("255.0.255.0");
    assert(!xg_lan_on_link(ip("192.168.1.21"), &invalid, 1));
    invalid.mask = ip("255.255.255.255");
    assert(!xg_lan_on_link(ip("192.168.1.21"), &invalid, 1));

    struct sockaddr_in address = {0};
#ifdef __APPLE__
    address.sin_len = sizeof(address);
#endif
    address.sin_family = AF_INET;
    address.sin_port = htons(5150);
    address.sin_addr.s_addr = ip("192.168.1.21");
    unsigned char unaligned[sizeof(address) + 1];
    memcpy(unaligned + 1, &address, sizeof(address));
    assert(xg_lan_resolved_ipv4(unaligned + 1, sizeof(address), nets, 4) == address.sin_addr.s_addr);
    assert(!xg_lan_resolved_ipv4(unaligned + 1, sizeof(address) - 1, nets, 4));
    assert(!xg_lan_resolved_ipv4(NULL, sizeof(address), nets, 4));
    address.sin_port = htons(443);
    assert(!xg_lan_resolved_ipv4(&address, sizeof(address), nets, 4));
    address.sin_port = htons(5150); address.sin_family = AF_INET6;
    assert(!xg_lan_resolved_ipv4(&address, sizeof(address), nets, 4));
    address.sin_family = AF_INET;
#ifdef __APPLE__
    address.sin_len = 2;
    assert(!xg_lan_resolved_ipv4(&address, sizeof(address), nets, 4));
#endif

    uint32_t a = ip("192.168.1.21"), b = ip("192.168.1.22");
    uint32_t peers[] = {a, a, 0, b}, output[XG_LAN_MAX_PEERS + 8] = {0};
    xg_lan_cache_replace(&cache, peers, 4);
    assert(xg_lan_cache_snapshot(&cache, output, 40) == 2);
    assert(output[0] == a && output[1] == b);
    output[1] = 0xfeedbeef;
    assert(xg_lan_cache_snapshot(&cache, output, 1) == 1 && output[1] == 0xfeedbeef);
    assert(!xg_lan_cache_snapshot(&cache, NULL, 40));
    assert(!xg_lan_cache_snapshot(&cache, output, 0));
    xg_lan_cache_replace(&cache, &b, 1); /* removal replaces, not appends */
    assert(xg_lan_cache_snapshot(&cache, output, 40) == 1 && output[0] == b);
    xg_lan_cache_replace(&cache, NULL, 0); /* background stop clears */
    assert(!xg_lan_cache_snapshot(&cache, output, 40));
    for (size_t i = 0; i < XG_LAN_MAX_PEERS + 8; i++) output[i] = (uint32_t)i + 1;
    xg_lan_cache_replace(&cache, output, XG_LAN_MAX_PEERS + 8);
    assert(xg_lan_cache_snapshot(&cache, output, XG_LAN_MAX_PEERS + 8) == XG_LAN_MAX_PEERS);
    xg_lan_cache_replace(&cache, NULL, 0);
    pthread_t thread;
    assert(!pthread_create(&thread, NULL, writer, NULL));
    for (int i = 0; i < 20000; i++) {
        size_t count = xg_lan_cache_snapshot(&cache, output, 40);
        assert(count == 0 || (count == 2 && output[0] == 4 && output[1] == 5) ||
            (count == 3 && output[0] == 1 && output[1] == 2 && output[2] == 3));
    }
    assert(!pthread_join(thread, NULL));
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='halopad-lan-peers-') as folder:
            path = Path(folder)
            (path / 'test.c').write_text(source)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                            '-I', str(ROOT / 'port/xbox'), str(path / 'test.c'), '-o', str(path / 'test')],
                           check=True, capture_output=True)
            subprocess.run([str(path / 'test')], check=True, capture_output=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
