/* Bonjour supplies local unicast peers; the game still owns discovery packets. */
#import <UIKit/UIKit.h>
#include <dns_sd.h>
#include <ifaddrs.h>
#include <net/if.h>
#include "xg_host.h"
#include "xg_lan.h"
#include "xg_lan_peers.h"

static NSString *const service_type = @"_halopad._udp.";
static struct xg_lan_cache peer_cache = XG_LAN_CACHE_INITIALIZER;

static size_t local_interfaces(struct xg_lan_interface *out, size_t capacity)
{
	struct ifaddrs *list = NULL;
	size_t count = 0;
	if (getifaddrs(&list)) return 0;
	for (struct ifaddrs *item = list; item && count < capacity; item = item->ifa_next) {
		unsigned required = IFF_UP | IFF_RUNNING | IFF_BROADCAST;
		if (!item->ifa_addr || !item->ifa_netmask || item->ifa_addr->sa_family != AF_INET ||
			item->ifa_netmask->sa_family != AF_INET || (item->ifa_flags & required) != required ||
			(item->ifa_flags & IFF_LOOPBACK)) continue;
		out[count++] = (struct xg_lan_interface){
			((struct sockaddr_in *)item->ifa_addr)->sin_addr.s_addr,
			((struct sockaddr_in *)item->ifa_netmask)->sin_addr.s_addr};
	}
	freeifaddrs(list);
	return count;
}

static NSString *service_key(NSNetService *service)
{
	return [NSString stringWithFormat:@"%@/%@/%@", service.domain, service.type, service.name];
}

@class XGLANDiscovery;
@interface XGLANAddressQuery : NSObject
@property DNSServiceRef reference;
@property(nonatomic, copy) NSString *hostname;
@property(nonatomic, strong) NSNetService *service;
@property(nonatomic, weak) XGLANDiscovery *owner;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSData *> *addresses;
- (void)stop;
@end
@implementation XGLANAddressQuery
- (void)stop
{
	/* Creation, callbacks and cancellation all run on the main queue. */
	if (self.reference) DNSServiceRefDeallocate(self.reference);
	self.reference = NULL;
}
@end

@interface XGLANDiscovery : NSObject <NSNetServiceDelegate, NSNetServiceBrowserDelegate>
@property BOOL wanted;
@property NSUInteger retryCount;
@property NSUInteger peerCount;
@property(nonatomic, strong) NSNetService *publisher;
@property(nonatomic, strong) NSNetServiceBrowser *browser;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSNetService *> *services;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSArray<NSData *> *> *resolved;
@property(nonatomic, strong) NSMutableDictionary<NSString *, XGLANAddressQuery *> *queries;
@property(nonatomic, strong) NSTimer *refresh;
- (void)start;
- (void)stop;
- (void)addressQuery:(XGLANAddressQuery *)query reference:(DNSServiceRef)reference
	flags:(DNSServiceFlags)flags interface:(uint32_t)interface error:(DNSServiceErrorType)error
	address:(const struct sockaddr *)address;
@end

static void resolved_ipv4(DNSServiceRef reference, DNSServiceFlags flags, uint32_t interface,
	DNSServiceErrorType error, const char *hostname, const struct sockaddr *address, uint32_t ttl, void *context)
{
	(void)hostname; (void)ttl;
	XGLANAddressQuery *query = (__bridge XGLANAddressQuery *)context;
	[query.owner addressQuery:query reference:reference flags:flags interface:interface error:error address:address];
}

@implementation XGLANDiscovery
- (instancetype)init
{
	if ((self = [super init])) {
		_services = [NSMutableDictionary new];
		_resolved = [NSMutableDictionary new];
		_queries = [NSMutableDictionary new];
		[NSNotificationCenter.defaultCenter addObserver:self selector:@selector(background:)
			name:UIApplicationDidEnterBackgroundNotification object:nil];
		[NSNotificationCenter.defaultCenter addObserver:self selector:@selector(foreground:)
			name:UIApplicationDidBecomeActiveNotification object:nil];
	}
	return self;
}

- (void)start
{
	self.wanted = YES;
	if (self.browser || UIApplication.sharedApplication.applicationState == UIApplicationStateBackground) return;
	/* A peer marker, not a second listener: OpenCE owns UDP 5150/5151. */
	self.publisher = [[NSNetService alloc] initWithDomain:@"local." type:service_type
		name:NSUUID.UUID.UUIDString port:5150];
	self.publisher.delegate = self;
	self.publisher.includesPeerToPeer = NO;
	[self.publisher scheduleInRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	[self.publisher publish];
	self.browser = [NSNetServiceBrowser new];
	self.browser.delegate = self;
	self.browser.includesPeerToPeer = NO;
	[self.browser scheduleInRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	[self.browser searchForServicesOfType:service_type inDomain:@"local."];
	self.refresh = [NSTimer timerWithTimeInterval:15 target:self selector:@selector(resolveAgain:)
		userInfo:nil repeats:YES];
	[NSRunLoop.mainRunLoop addTimer:self.refresh forMode:NSRunLoopCommonModes];
	xg_log("LAN Bonjour: started");
}

- (void)clear
{
	BOOL running = self.browser != nil || self.publisher != nil;
	[self.refresh invalidate]; self.refresh = nil;
	self.browser.delegate = nil; [self.browser stop];
	[self.browser removeFromRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes]; self.browser = nil;
	self.publisher.delegate = nil; [self.publisher stop];
	[self.publisher removeFromRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes]; self.publisher = nil;
	for (NSNetService *service in self.services.allValues) {
		service.delegate = nil; [service stop];
		[service removeFromRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	}
	[self.services removeAllObjects];
	for (XGLANAddressQuery *query in self.queries.allValues) [query stop];
	[self.queries removeAllObjects];
	[self.resolved removeAllObjects];
	xg_lan_cache_replace(&peer_cache, NULL, 0);
	if (self.peerCount) xg_log("LAN Bonjour: 0 peers");
	self.peerCount = 0;
	if (running) xg_log("LAN Bonjour: stopped");
}
- (void)stop { self.wanted = NO; [self clear]; }
- (void)background:(NSNotification *)notification { (void)notification; [self clear]; }
- (void)foreground:(NSNotification *)notification
{
	(void)notification;
	if (self.wanted) { [self clear]; self.retryCount = 0; [self start]; }
}

- (void)updateSnapshot
{
	struct xg_lan_interface interfaces[64];
	size_t interface_count = local_interfaces(interfaces, 64), count = 0;
	uint32_t addresses[XG_LAN_MAX_PEERS];
	for (NSArray<NSData *> *results in self.resolved.allValues) {
		for (NSData *data in results) {
			uint32_t ip = xg_lan_resolved_ipv4(data.bytes, data.length, interfaces, interface_count);
			if (!ip) continue;
			size_t index;
			for (index = 0; index < count && addresses[index] != ip; index++) {}
			if (index == count && count < XG_LAN_MAX_PEERS) addresses[count++] = ip;
		}
	}
	xg_lan_cache_replace(&peer_cache, addresses, count);
	if (count != self.peerCount) xg_log("LAN Bonjour: %zu peers", count);
	self.peerCount = count;
}

- (void)resolveAgain:(NSTimer *)timer
{
	(void)timer;
	/* Drop addresses no longer on an active local interface before resolving. */
	[self updateSnapshot];
	for (NSNetService *service in self.services.allValues) {
		[service stop];
		[service resolveWithTimeout:5];
	}
}

- (void)netServiceBrowser:(NSNetServiceBrowser *)browser didFindService:(NSNetService *)service moreComing:(BOOL)more
{
	(void)more;
	if (browser != self.browser || ![service.domain isEqualToString:@"local."] ||
		![service.type isEqualToString:service_type] || [service.name isEqualToString:self.publisher.name]) return;
	NSString *key = service_key(service);
	if (self.services[key] || self.services.count >= XG_LAN_MAX_PEERS) return;
	self.services[key] = service;
	service.delegate = self;
	service.includesPeerToPeer = NO;
	[service scheduleInRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	[service resolveWithTimeout:5];
}
- (void)netServiceBrowser:(NSNetServiceBrowser *)browser didRemoveService:(NSNetService *)service moreComing:(BOOL)more
{
	(void)more;
	if (browser != self.browser) return;
	NSString *key = service_key(service);
	NSNetService *existing = self.services[key];
	existing.delegate = nil; [existing stop];
	[existing removeFromRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	[self.services removeObjectForKey:key];
	[self.queries[key] stop]; [self.queries removeObjectForKey:key];
	[self.resolved removeObjectForKey:key];
	[self updateSnapshot];
}
- (void)netServiceDidResolveAddress:(NSNetService *)service
{
	NSString *key = service_key(service);
	if (!self.browser || self.services[key] != service) return;
	NSString *hostname = service.hostName;
	/* NSNetService may choose an IPv6-only USB interface even when this host
	 * also has a Wi-Fi IPv4 address. Query A records across active interfaces.
	 * Only a local hostname and our fixed marker port may supply game peers. */
	BOOL local = hostname.length > 7 && [hostname.lowercaseString hasSuffix:@".local."] &&
		strlen(hostname.UTF8String) == [hostname lengthOfBytesUsingEncoding:NSUTF8StringEncoding];
	XGLANAddressQuery *query = self.queries[key];
	if (local && service.port == 5150 && query.reference && [query.hostname isEqualToString:hostname]) return;
	[query stop]; [self.queries removeObjectForKey:key];
	[self.resolved removeObjectForKey:key];
	[self updateSnapshot];
	if (!local || service.port != 5150) return;
	query = [XGLANAddressQuery new];
	query.hostname = hostname; query.service = service; query.owner = self;
	query.addresses = [NSMutableDictionary new];
	DNSServiceRef reference = NULL;
	DNSServiceErrorType error = DNSServiceGetAddrInfo(&reference, 0, kDNSServiceInterfaceIndexAny,
		kDNSServiceProtocol_IPv4, hostname.UTF8String, resolved_ipv4, (__bridge void *)query);
	query.reference = reference;
	self.queries[key] = query;
	if (!error) error = DNSServiceSetDispatchQueue(reference, dispatch_get_main_queue());
	if (error) {
		xg_log("LAN Bonjour: IPv4 query failed (%d)", error);
		[query stop]; [self.queries removeObjectForKey:key];
	}
}
- (void)addressQuery:(XGLANAddressQuery *)query reference:(DNSServiceRef)reference
	flags:(DNSServiceFlags)flags interface:(uint32_t)interface error:(DNSServiceErrorType)error
	address:(const struct sockaddr *)address
{
	NSString *key = service_key(query.service);
	if (!self.browser || self.services[key] != query.service || self.queries[key] != query ||
		query.reference != reference) return;
	if (error) {
		xg_log("LAN Bonjour: IPv4 query failed (%d)", error);
		[query stop]; [self.queries removeObjectForKey:key];
		[self.resolved removeObjectForKey:key];
	} else {
		if (!address || address->sa_family != AF_INET || address->sa_len != sizeof(struct sockaddr_in)) return;
		struct sockaddr_in ipv4;
		memcpy(&ipv4, address, sizeof(ipv4));
		ipv4.sin_port = htons(5150);
		/* One address may be reported on multiple interfaces. Removing one
		 * interface must not discard the still-advertised copy on another. */
		NSString *record = [NSString stringWithFormat:@"%u/%u", interface, ipv4.sin_addr.s_addr];
		if (!(flags & kDNSServiceFlagsAdd)) [query.addresses removeObjectForKey:record];
		else if (query.addresses.count < XG_LAN_MAX_PEERS || query.addresses[record])
			query.addresses[record] = [NSData dataWithBytes:&ipv4 length:sizeof(ipv4)];
		self.resolved[key] = query.addresses.allValues;
	}
	[self updateSnapshot];
}
- (void)netService:(NSNetService *)service didNotResolve:(NSDictionary *)error
{
	NSString *key = service_key(service);
	if (self.services[key] != service) return;
	xg_log("LAN Bonjour: resolve failed (%ld)", (long)[error[NSNetServicesErrorCode] integerValue]);
	[self.queries[key] stop]; [self.queries removeObjectForKey:key];
	[self.resolved removeObjectForKey:key];
	[self updateSnapshot];
}
- (void)netServiceBrowser:(NSNetServiceBrowser *)browser didNotSearch:(NSDictionary *)error
{
	if (browser != self.browser) return;
	xg_log("LAN Bonjour: search failed (%ld)", (long)[error[NSNetServicesErrorCode] integerValue]);
	[self retryAfterFailure];
}
- (void)netService:(NSNetService *)service didNotPublish:(NSDictionary *)error
{
	if (service != self.publisher) return;
	xg_log("LAN Bonjour: publish failed (%ld)", (long)[error[NSNetServicesErrorCode] integerValue]);
	[self retryAfterFailure];
}
- (void)retryAfterFailure
{
	[self clear];
	/* A first operation can fail while the permission sheet is being answered.
	 * Retry briefly, then wait for foreground activation after Settings. */
	if (self.retryCount++ >= 2) return;
	dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
		if (self.wanted && !self.browser) [self start];
	});
}
@end

static XGLANDiscovery *discovery(void)
{
	static XGLANDiscovery *instance;
	static dispatch_once_t once;
	dispatch_once(&once, ^{ instance = [XGLANDiscovery new]; });
	return instance;
}
void xg_lan_start(void)
{
	if (NSThread.isMainThread) [discovery() start];
	else dispatch_async(dispatch_get_main_queue(), ^{ [discovery() start]; });
}
void xg_lan_stop(void)
{
	if (NSThread.isMainThread) [discovery() stop];
	else dispatch_async(dispatch_get_main_queue(), ^{ [discovery() stop]; });
}
size_t xg_lan_snapshot(uint32_t *addresses, size_t capacity)
{
	return xg_lan_cache_snapshot(&peer_cache, addresses, capacity);
}
