/* Exercise the shipped Objective-C manager; replace only OS network/UI edges. */
#import <Foundation/Foundation.h>
#include <assert.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdarg.h>

static NSUInteger live_publishers, live_browsers, searches;
@interface TestService : NSNetService
@property(nonatomic, strong) NSArray<NSData *> *testAddresses;
@property BOOL publishing;
@property NSUInteger resolves;
@end
@implementation TestService
- (NSArray<NSData *> *)addresses { return self.testAddresses; }
- (void)publish { if (!self.publishing) { self.publishing = YES; live_publishers++; } }
- (void)stop { if (self.publishing) { self.publishing = NO; live_publishers--; } }
- (void)resolveWithTimeout:(NSTimeInterval)timeout { (void)timeout; self.resolves++; }
- (void)scheduleInRunLoop:(NSRunLoop *)runLoop forMode:(NSString *)mode { (void)runLoop; (void)mode; }
- (void)removeFromRunLoop:(NSRunLoop *)runLoop forMode:(NSString *)mode { (void)runLoop; (void)mode; }
@end
@interface TestBrowser : NSNetServiceBrowser
@property BOOL searching;
@end
@implementation TestBrowser
- (void)searchForServicesOfType:(NSString *)type inDomain:(NSString *)domain
{
    assert([type isEqualToString:@"_halopad._udp."] && [domain isEqualToString:@"local."]);
    if (!self.searching) { self.searching = YES; live_browsers++; searches++; }
}
- (void)stop { if (self.searching) { self.searching = NO; live_browsers--; } }
- (void)scheduleInRunLoop:(NSRunLoop *)runLoop forMode:(NSString *)mode { (void)runLoop; (void)mode; }
- (void)removeFromRunLoop:(NSRunLoop *)runLoop forMode:(NSString *)mode { (void)runLoop; (void)mode; }
@end

static int test_getifaddrs(struct ifaddrs **result)
{
    static struct sockaddr_in address, mask;
    static struct ifaddrs item;
    address.sin_len = mask.sin_len = sizeof(address);
    address.sin_family = mask.sin_family = AF_INET;
    inet_pton(AF_INET, "192.168.50.10", &address.sin_addr);
    inet_pton(AF_INET, "255.255.255.0", &mask.sin_addr);
    item.ifa_addr = (struct sockaddr *)&address;
    item.ifa_netmask = (struct sockaddr *)&mask;
    item.ifa_flags = IFF_UP | IFF_RUNNING | IFF_BROADCAST;
    *result = &item;
    return 0;
}
static void test_freeifaddrs(struct ifaddrs *items) { (void)items; }
void xg_log(const char *format, ...) { (void)format; }

#define NSNetService TestService
#define NSNetServiceBrowser TestBrowser
#define getifaddrs test_getifaddrs
#define freeifaddrs test_freeifaddrs
#include "xg_lan.m"

NSString *const UIApplicationDidEnterBackgroundNotification = @"TestBackground";
NSString *const UIApplicationDidBecomeActiveNotification = @"TestForeground";
@implementation UIApplication
+ (instancetype)sharedApplication
{
    static UIApplication *app;
    if (!app) app = [UIApplication new];
    return app;
}
@end

static TestService *peer(NSString *name, const char *ip)
{
    TestService *service = [[TestService alloc] initWithDomain:@"local." type:@"_halopad._udp." name:name port:5150];
    struct sockaddr_in address = {0};
    address.sin_len = sizeof(address);
    address.sin_family = AF_INET; address.sin_port = htons(5150);
    assert(inet_pton(AF_INET, ip, &address.sin_addr) == 1);
    service.testAddresses = @[[NSData dataWithBytes:&address length:sizeof(address)]];
    return service;
}

int main(void)
{
    @autoreleasepool {
        uint32_t output[XG_LAN_MAX_PEERS];
        UIApplication.sharedApplication.applicationState = UIApplicationStateActive;
        xg_lan_start();
        XGLANDiscovery *manager = discovery();
        assert(live_publishers == 1 && live_browsers == 1 && searches == 1);
        xg_lan_start(); assert(searches == 1); /* idempotent */
        TestService *first = peer(@"peer", "192.168.50.11");
        [manager netServiceBrowser:manager.browser didFindService:first moreComing:NO];
        assert(first.resolves == 1);
        [manager netServiceDidResolveAddress:first];
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1);
        [manager netServiceBrowser:manager.browser didRemoveService:first moreComing:NO];
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        [manager netServiceDidResolveAddress:first]; /* late result after removal */
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));

        [manager netServiceBrowser:manager.browser didFindService:first moreComing:NO];
        [manager netServiceDidResolveAddress:first];
        TestBrowser *oldBrowser = manager.browser;
        UIApplication.sharedApplication.applicationState = UIApplicationStateBackground;
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidEnterBackgroundNotification object:nil];
        assert(manager.wanted && !manager.browser && !manager.publisher);
        assert(!live_publishers && !live_browsers && !xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        [manager netServiceDidResolveAddress:first]; /* late result after clear */
        [manager netServiceBrowser:oldBrowser didFindService:first moreComing:NO];
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS) && !manager.services.count);

        UIApplication.sharedApplication.applicationState = UIApplicationStateActive;
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidBecomeActiveNotification object:nil];
        assert(manager.browser != oldBrowser && live_publishers == 1 && live_browsers == 1 && searches == 2);
        TestService *replacement = peer(@"peer", "192.168.50.12");
        [manager netServiceBrowser:manager.browser didFindService:replacement moreComing:NO];
        [manager netServiceDidResolveAddress:replacement];
        [manager netServiceDidResolveAddress:first]; /* same name, stale object */
        [manager netServiceBrowser:oldBrowser didRemoveService:first moreComing:NO];
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1);
        assert(output[0] == inet_addr("192.168.50.12"));
        [manager netService:replacement didNotResolve:@{}];
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        xg_lan_stop();
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidBecomeActiveNotification object:nil];
        assert(!manager.wanted && !live_publishers && !live_browsers && searches == 2);
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
    }
    return 0;
}
