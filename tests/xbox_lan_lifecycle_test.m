/* Exercise the shipped Objective-C manager; replace only OS network/UI edges. */
#import <Foundation/Foundation.h>
#include <assert.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdarg.h>
#include <dns_sd.h>

struct TestQuery {
    DNSServiceGetAddrInfoReply callback;
    void *context;
    BOOL active;
};
static struct TestQuery testQueries[64];
static NSUInteger queryCount, liveQueries;
static DNSServiceErrorType queryError, scheduleError;
static DNSServiceErrorType test_getaddrinfo(DNSServiceRef *reference, DNSServiceFlags flags,
    uint32_t interface, DNSServiceProtocol protocol, const char *hostname,
    DNSServiceGetAddrInfoReply callback, void *context)
{
    assert(!flags && interface == kDNSServiceInterfaceIndexAny && protocol == kDNSServiceProtocol_IPv4);
    assert(hostname && strstr(hostname, ".local."));
    if (queryError) return queryError;
    assert(queryCount < 64);
    struct TestQuery *query = &testQueries[queryCount++];
    *query = (struct TestQuery){callback, context, YES};
    *reference = (DNSServiceRef)query;
    liveQueries++;
    return 0;
}
static DNSServiceErrorType test_schedule(DNSServiceRef reference, dispatch_queue_t queue)
{
    assert(reference && queue == dispatch_get_main_queue());
    return scheduleError;
}
static void test_deallocate(DNSServiceRef reference)
{
    assert(NSThread.isMainThread);
    struct TestQuery *query = (struct TestQuery *)reference;
    assert(query->active && liveQueries);
    query->active = NO; liveQueries--;
}

static NSUInteger live_publishers, live_browsers, searches;
@interface TestService : NSNetService
@property(nonatomic, strong) NSArray<NSData *> *testAddresses;
@property(nonatomic, copy) NSString *testHostname;
@property NSInteger testPort;
@property BOOL publishing;
@property NSUInteger resolves;
@end
@implementation TestService
- (NSArray<NSData *> *)addresses { return self.testAddresses; }
- (NSString *)hostName { return self.testHostname; }
- (NSInteger)port { return self.testPort; }
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
#define DNSServiceGetAddrInfo test_getaddrinfo
#define DNSServiceSetDispatchQueue test_schedule
#define DNSServiceRefDeallocate test_deallocate
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
    service.testHostname = [name stringByAppendingString:@".local."];
    service.testPort = 5150;
    return service;
}

static void address_event(XGLANAddressQuery *query, uint32_t interface, const char *ip, BOOL add)
{
    struct sockaddr_in address = {0};
    address.sin_len = sizeof(address); address.sin_family = AF_INET;
    assert(inet_pton(AF_INET, ip, &address.sin_addr) == 1);
    struct TestQuery *call = (struct TestQuery *)query.reference;
    assert(call && call->active);
    call->callback(query.reference, add ? kDNSServiceFlagsAdd : 0, interface, 0,
        query.hostname.UTF8String, (const struct sockaddr *)&address, 120, call->context);
}
static XGLANAddressQuery *resolve(XGLANDiscovery *manager, TestService *service, const char *ip)
{
    [manager netServiceDidResolveAddress:service];
    XGLANAddressQuery *query = manager.queries[service_key(service)];
    assert(query && query.reference);
    address_event(query, 14, ip, YES);
    return query;
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
        /* Reproduce the physical bug: Foundation chooses an IPv6-only USB
         * interface while an all-interface A query finds the Wi-Fi address. */
        struct sockaddr_in6 v6 = {0}; v6.sin6_len = sizeof(v6); v6.sin6_family = AF_INET6;
        first.testAddresses = @[[NSData dataWithBytes:&v6 length:sizeof(v6)]];
        XGLANAddressQuery *firstQuery = resolve(manager, first, "192.168.50.11");
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1);
        assert(output[0] == inet_addr("192.168.50.11"));
        NSUInteger initialQueries = queryCount;
        [manager resolveAgain:nil];
        [manager netServiceDidResolveAddress:first];
        assert(queryCount == initialQueries && liveQueries == 1);
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1); /* refresh retains live A query */
        address_event(firstQuery, 18, "192.168.50.11", YES);
        address_event(firstQuery, 14, "192.168.50.11", NO);
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1); /* other interface still advertises it */
        address_event(firstQuery, 18, "192.168.50.11", NO);
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        address_event(firstQuery, 14, "192.168.50.11", YES);
        address_event(firstQuery, 14, "8.8.8.8", YES);
        address_event(firstQuery, 14, "192.168.50.255", YES);
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1); /* existing on-link validation */
        DNSServiceRef removedReference = firstQuery.reference;
        [manager netServiceBrowser:manager.browser didRemoveService:first moreComing:NO];
        assert(!liveQueries && !xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        [manager addressQuery:firstQuery reference:removedReference flags:0 interface:0 error:-65537 address:NULL];
        [manager netServiceDidResolveAddress:first]; /* late result after removal */
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));

        [manager netServiceBrowser:manager.browser didFindService:first moreComing:NO];
        firstQuery = resolve(manager, first, "192.168.50.11");
        removedReference = firstQuery.reference;
        TestBrowser *oldBrowser = manager.browser;
        UIApplication.sharedApplication.applicationState = UIApplicationStateBackground;
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidEnterBackgroundNotification object:nil];
        assert(manager.wanted && !manager.browser && !manager.publisher);
        assert(!live_publishers && !live_browsers && !liveQueries && !xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        [manager addressQuery:firstQuery reference:removedReference flags:0 interface:0 error:-65537 address:NULL];
        [manager netServiceDidResolveAddress:first]; /* late result after clear */
        [manager netServiceBrowser:oldBrowser didFindService:first moreComing:NO];
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS) && !manager.services.count);

        UIApplication.sharedApplication.applicationState = UIApplicationStateActive;
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidBecomeActiveNotification object:nil];
        assert(manager.browser != oldBrowser && live_publishers == 1 && live_browsers == 1 && searches == 2);
        TestService *replacement = peer(@"peer", "192.168.50.12");
        [manager netServiceBrowser:manager.browser didFindService:replacement moreComing:NO];
        XGLANAddressQuery *replacementQuery = resolve(manager, replacement, "192.168.50.12");
        [manager netServiceDidResolveAddress:first]; /* same name, stale object */
        [manager netServiceBrowser:oldBrowser didRemoveService:first moreComing:NO];
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1);
        assert(output[0] == inet_addr("192.168.50.12"));
        DNSServiceRef staleReference = replacementQuery.reference;
        replacement.testHostname = @"moved.local.";
        XGLANAddressQuery *movedQuery = resolve(manager, replacement, "192.168.50.13");
        assert(movedQuery != replacementQuery && liveQueries == 1);
        [manager addressQuery:replacementQuery reference:staleReference flags:0 interface:0 error:-65537 address:NULL];
        assert(xg_lan_snapshot(output, XG_LAN_MAX_PEERS) == 1 && output[0] == inet_addr("192.168.50.13"));
        [manager netService:replacement didNotResolve:@{}];
        assert(!liveQueries && !xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        replacement.testHostname = @"example.com.";
        [manager netServiceDidResolveAddress:replacement]; assert(!liveQueries);
        replacement.testHostname = @"moved.local."; replacement.testPort = 443;
        [manager netServiceDidResolveAddress:replacement]; assert(!liveQueries);
        replacement.testPort = 5150;
        queryError = kDNSServiceErr_NoMemory;
        [manager netServiceDidResolveAddress:replacement]; assert(!liveQueries);
        queryError = 0; scheduleError = kDNSServiceErr_BadParam;
        [manager netServiceDidResolveAddress:replacement]; assert(!liveQueries);
        scheduleError = 0;
        movedQuery = resolve(manager, replacement, "192.168.50.13");
        [manager addressQuery:movedQuery reference:movedQuery.reference flags:0 interface:0 error:-65537 address:NULL];
        assert(!liveQueries && !xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
        resolve(manager, replacement, "192.168.50.13");
        xg_lan_stop();
        [NSNotificationCenter.defaultCenter postNotificationName:UIApplicationDidBecomeActiveNotification object:nil];
        assert(!manager.wanted && !live_publishers && !live_browsers && !liveQueries && searches == 2);
        assert(!xg_lan_snapshot(output, XG_LAN_MAX_PEERS));
    }
    return 0;
}
