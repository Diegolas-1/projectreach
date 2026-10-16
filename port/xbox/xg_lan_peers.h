/* Internal IPv4 validation and snapshot storage, shared with native tests. */
#ifndef XG_LAN_PEERS_H
#define XG_LAN_PEERS_H
#include <arpa/inet.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include "xg_lan.h"

struct xg_lan_interface { uint32_t address, mask; };
struct xg_lan_cache {
	pthread_mutex_t lock;
	uint32_t addresses[XG_LAN_MAX_PEERS];
	size_t count;
};
#define XG_LAN_CACHE_INITIALIZER { PTHREAD_MUTEX_INITIALIZER, {0}, 0 }

static int xg_lan_on_link(uint32_t address, const struct xg_lan_interface *interfaces, size_t count)
{
	uint32_t ip = ntohl(address);
	if ((ip >> 24) == 0 || (ip >> 24) == 127 || ip >= 0xe0000000u)
		return 0;
	for (size_t i = 0; i < count; i++)
		if (address == interfaces[i].address) return 0;
	for (size_t i = 0; i < count; i++) {
		uint32_t mask = ntohl(interfaces[i].mask), host = ~mask;
		uint32_t local = ntohl(interfaces[i].address), network = local & mask;
		/* A valid contiguous mask; /31 has two usable endpoints. */
		if (!mask || (host & (host + 1)) || (ip & mask) != network) continue;
		if (host > 1 && (ip == network || ip == (network | host))) continue;
		return 1;
	}
	return 0;
}

static uint32_t xg_lan_resolved_ipv4(const void *bytes, size_t length,
	const struct xg_lan_interface *interfaces, size_t count)
{
	struct sockaddr_in address;
	if (!bytes || length < sizeof(address)) return 0;
	/* NSData's bytes need not be aligned as a sockaddr_in. */
	memcpy(&address, bytes, sizeof(address));
	if (address.sin_family != AF_INET || ntohs(address.sin_port) != 5150) return 0;
#ifdef __APPLE__
	if (address.sin_len != sizeof(address)) return 0;
#endif
	return xg_lan_on_link(address.sin_addr.s_addr, interfaces, count) ? address.sin_addr.s_addr : 0;
}

static void xg_lan_cache_replace(struct xg_lan_cache *cache, const uint32_t *addresses, size_t count)
{
	pthread_mutex_lock(&cache->lock);
	cache->count = 0;
	for (size_t i = 0; addresses && i < count && cache->count < XG_LAN_MAX_PEERS; i++) {
		size_t j;
		if (!addresses[i]) continue;
		for (j = 0; j < cache->count && cache->addresses[j] != addresses[i]; j++) {}
		if (j == cache->count) cache->addresses[cache->count++] = addresses[i];
	}
	pthread_mutex_unlock(&cache->lock);
}

static size_t xg_lan_cache_snapshot(struct xg_lan_cache *cache, uint32_t *addresses, size_t capacity)
{
	size_t count;
	if (!addresses || !capacity) return 0;
	pthread_mutex_lock(&cache->lock);
	count = cache->count < capacity ? cache->count : capacity;
	memcpy(addresses, cache->addresses, count * sizeof(*addresses));
	pthread_mutex_unlock(&cache->lock);
	return count;
}
#endif
