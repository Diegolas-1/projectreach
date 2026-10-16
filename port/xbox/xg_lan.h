/* Bonjour peers for the UIKit/Catalyst Xbox host. */
#ifndef XG_LAN_H
#define XG_LAN_H
#include <stddef.h>
#include <stdint.h>

#define XG_LAN_MAX_PEERS 128

/* Idempotent; Bonjour and its lifecycle callbacks run on the main queue. */
void xg_lan_start(void);
void xg_lan_stop(void);
/* Nonblocking apart from a short mutex; addresses are in network byte order. */
size_t xg_lan_snapshot(uint32_t *addresses, size_t capacity);
#endif
