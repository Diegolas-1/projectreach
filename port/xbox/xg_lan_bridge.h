/* LAN address selection shared by the POSIX bridge and its native tests. */
#ifndef XG_LAN_BRIDGE_H
#define XG_LAN_BRIDGE_H

#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>

static inline uint32_t xg_lan_preferred_address(const struct ifaddrs *interfaces,
    uint32_t routed)
{
    uint32_t lan = 0, link_local = 0;
    for (const struct ifaddrs *entry = interfaces; entry; entry = entry->ifa_next) {
        unsigned required = IFF_UP | IFF_RUNNING | IFF_BROADCAST;
        if (!entry->ifa_addr || entry->ifa_addr->sa_family != AF_INET ||
            (entry->ifa_flags & required) != required ||
            (entry->ifa_flags & (IFF_LOOPBACK | IFF_POINTOPOINT))) continue;
        uint32_t address = ((const struct sockaddr_in *)entry->ifa_addr)->sin_addr.s_addr;
        uint32_t host = ntohl(address);
        if (!(host >> 24) || (host >> 24) == 127 || (host >> 24) >= 224) continue;
        if ((host >> 16) == 0xa9fe) {
            if (!link_local || address == routed) link_local = address;
        } else {
            /* Keep the default route when it already belongs to a usable LAN. */
            if (address == routed) return routed;
            if (!lan) lan = address;
        }
    }
    /* Wi-Fi without internet must win over cellular/VPN or a USB address.
       Link-local LANs still work when no ordinary LAN address is available. */
    return lan ? lan : link_local ? link_local : routed;
}

#endif
