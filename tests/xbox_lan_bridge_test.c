#include <assert.h>
#include <arpa/inet.h>
#include <pthread.h>
#include <unistd.h>
#define getifaddrs test_getifaddrs
#define freeifaddrs test_freeifaddrs
#include "xg_posix.c"

uintptr_t xg_base;
__thread int xg_errno;
int xg_linux_errno(int value) { return value; }
static unsigned char memory[512];
#define SEND_CAPACITY (XG_LAN_MAX_PEERS + 8)
static uint32_t peers[XG_LAN_MAX_PEERS];
static size_t peer_count, snapshots;
static int send_count, results[SEND_CAPACITY], errors[SEND_CAPACITY];
static __thread int upstream_error;
static int select_result;
static struct sockaddr_storage destinations[SEND_CAPACITY];
static int destination_lengths[SEND_CAPACITY];
static int expected_socket;
static struct ifaddrs interfaces[8];
static struct sockaddr_in local_addresses[8];
static int interface_count, interface_failure, interface_frees;
static uint32_t routed;

size_t xg_lan_snapshot(uint32_t *addresses, size_t capacity)
{
    assert(capacity == XG_LAN_MAX_PEERS);
    memcpy(addresses, peers, capacity * sizeof(*addresses));
    snapshots++;
    return peer_count;
}
int posix_socket_last_error(void) { return upstream_error; }
int posix_socket_sendto(int s, const void *buffer, int length, int flags,
    const void *address, int address_length)
{
    assert(s == expected_socket && buffer == memory + 32 && length == 11 && flags == 4);
    int call = send_count++;
    assert(call < SEND_CAPACITY);
    destination_lengths[call] = address_length;
    if (address) memcpy(&destinations[call], address, (size_t)address_length);
    if (results[call] < 0) {
        errno = errors[call]; upstream_error = errors[call] == EACCES ? 10013 : 10065;
    } else upstream_error = 0;
    return results[call];
}
int posix_socket_recv(int s, void *buffer, int length, int flags)
{
    (void)s; (void)buffer; (void)length; (void)flags;
    errno = EAGAIN; upstream_error = 10035; return -1;
}
int posix_socket_recvfrom(int s, void *buffer, int length, int flags, void *address, int *size)
{
    (void)address; (void)size;
    return posix_socket_recv(s, buffer, length, flags);
}
int posix_socket_select(int *read, int *read_count, int *write, int *write_count,
    int *error, int *error_count, posix_long seconds, posix_long microseconds, int infinite)
{
    (void)read; (void)read_count; (void)write; (void)write_count; (void)error; (void)error_count;
    (void)seconds; (void)microseconds; (void)infinite;
    if (select_result > 0) upstream_error = 0;
    if (select_result < 0) { upstream_error = 10035; errno = EAGAIN; }
    return select_result;
}
int posix_make_directory(const char *path)
{
    (void)path; errno = EACCES; return -1;
}
posix_ulong posix_local_ipv4_address(void) { return routed; }
int test_getifaddrs(struct ifaddrs **out)
{
    if (interface_failure) return -1;
    *out = interface_count ? interfaces : NULL;
    return 0;
}
void test_freeifaddrs(struct ifaddrs *list) { (void)list; interface_frees++; }

static uint32_t ip(const char *text) { return inet_addr(text); }
static void reset(int socket, int port, uint32_t address)
{
    expected_socket = socket; send_count = 0; snapshots = 0; peer_count = 2;
    peers[0] = ip("192.168.5.20"); peers[1] = ip("192.168.5.21");
    memset(destinations, 0, sizeof(destinations));
    for (size_t i = 0; i < SEND_CAPACITY; i++) { results[i] = 11; errors[i] = EHOSTUNREACH; }
    struct sockaddr_in destination = {.sin_family = AF_INET, .sin_port = htons(port),
        .sin_addr.s_addr = address};
    memcpy(memory + 128, &destination, sizeof(destination));
    /* Guest Winsock/Linux family occupies two bytes; host Darwin has len,family. */
    memory[128] = AF_INET; memory[129] = 0;
    errno = EINTR;
}
static int send_packet(void)
{
    return xh_hostposix_socket_sendto(expected_socket, 32, 11, 4, 128, sizeof(struct sockaddr_in));
}
static const struct sockaddr_in *destination(int index)
{
    assert(destination_lengths[index] == sizeof(struct sockaddr_in));
    const struct sockaddr_in *result = (const struct sockaddr_in *)&destinations[index];
    assert(result->sin_family == AF_INET && result->sin_len == sizeof(*result));
    return result;
}
static void *other_thread(void *unused)
{
    (void)unused;
    assert(xh_hostposix_socket_last_error() == 0);
    assert(xh_hostposix_socket_recv(1, 32, 11, 0) == -1);
    assert(xh_hostposix_socket_last_error() == 10035);
    return NULL;
}
static void test_send_boundary(void)
{
    int udp = socket(AF_INET, SOCK_DGRAM, 0), tcp = socket(AF_INET, SOCK_STREAM, 0);
    assert(udp >= 0 && tcp >= 0);
    for (int port = 5150; port <= 5151; port++) {
        reset(udp, port, INADDR_BROADCAST);
        results[0] = -1; errors[0] = EACCES; results[2] = -1;
        assert(send_packet() == 11 && send_count == 3 && snapshots == 1);
        assert(destination(0)->sin_addr.s_addr == INADDR_BROADCAST);
        assert(destination(1)->sin_addr.s_addr == peers[0]);
        assert(destination(2)->sin_addr.s_addr == peers[1]);
        for (int i = 0; i < 3; i++) assert(destination(i)->sin_port == htons(port));
        assert(errno == EINTR && xg_errno == EINTR && xh_hostposix_socket_last_error() == 0);
        assert(upstream_error == 10065); /* Last peer failed; aggregate still succeeded. */
        assert(xh_hostposix_make_directory(32) == -1 && xh_hostposix_socket_last_error() == 0);
        pthread_t thread;
        assert(pthread_create(&thread, NULL, other_thread, NULL) == 0);
        assert(pthread_join(thread, NULL) == 0 && xh_hostposix_socket_last_error() == 0);
        select_result = 0;
        assert(xh_hostposix_socket_select(0, 0, 0, 0, 0, 0, 0, 0, 0) == 0);
        assert(xh_hostposix_socket_last_error() == 0 && upstream_error == 10065);
        select_result = -1;
        assert(xh_hostposix_socket_select(0, 0, 0, 0, 0, 0, 0, 0, 0) == -1);
        assert(xh_hostposix_socket_last_error() == 10035 && xg_errno == EAGAIN);
        assert(xh_hostposix_socket_recv(udp, 32, 11, 0) == -1);
        assert(xh_hostposix_socket_last_error() == 10035 && xg_errno == EAGAIN);
    }
    reset(udp, 5150, INADDR_BROADCAST);
    results[1] = results[2] = -1; /* Raw broadcast succeeded even though peers failed. */
    assert(send_packet() == 11 && xh_hostposix_socket_last_error() == 0 && errno == EINTR);
    select_result = 1;
    assert(xh_hostposix_socket_select(0, 0, 0, 0, 0, 0, 0, 0, 0) == 1);
    assert(xh_hostposix_socket_last_error() == 0);
    assert(xh_hostposix_socket_recvfrom(udp, 32, 11, 0, 0, 0) == -1);
    assert(xh_hostposix_socket_last_error() == 10035);

    reset(udp, 5150, INADDR_BROADCAST);
    results[0] = results[1] = results[2] = -1; errors[0] = EACCES;
    assert(send_packet() == -1 && send_count == 3);
    assert(errno == EACCES && xg_errno == EACCES && xh_hostposix_socket_last_error() == 10013);

    /* Manual unicast, unrelated ports, TCP and malformed/IPv6 destinations retain upstream behavior. */
    reset(udp, 5150, ip("192.168.5.30"));
    assert(send_packet() == 11 && send_count == 1 && snapshots == 0);
    assert(destination(0)->sin_addr.s_addr == ip("192.168.5.30"));
    reset(udp, 2302, INADDR_BROADCAST);
    assert(send_packet() == 11 && send_count == 1 && snapshots == 0);
    reset(tcp, 5150, INADDR_BROADCAST);
    assert(send_packet() == 11 && send_count == 1 && snapshots == 0);
    reset(-1, 5150, INADDR_BROADCAST);
    results[0] = -1; errors[0] = EACCES;
    assert(send_packet() == -1 && send_count == 1 && snapshots == 0);
    assert(xh_hostposix_socket_last_error() == 10013);
    reset(udp, 5150, INADDR_BROADCAST);
    assert(xh_hostposix_socket_sendto(udp, 32, 11, 4, 128, 1) == 11);
    assert(send_count == 1 && snapshots == 0 && destination_lengths[0] == 0);
    reset(udp, 5150, INADDR_BROADCAST);
    memory[128] = 10; /* Guest IPv6 family. */
    assert(send_packet() == 11 && send_count == 1 && snapshots == 0);
    assert(destinations[0].ss_family == AF_INET6);
    reset(udp, 5150, INADDR_BROADCAST);
    peer_count = 0;
    assert(send_packet() == 11 && send_count == 1);

    reset(udp, 5150, INADDR_BROADCAST);
    peers[1] = peers[0]; peers[2] = INADDR_ANY; peers[3] = INADDR_BROADCAST;
    peers[4] = ip("127.0.0.1"); peers[5] = ip("224.0.0.1"); peer_count = 6;
    assert(send_packet() == 11 && send_count == 2);
    reset(udp, 5150, INADDR_BROADCAST);
    for (size_t i = 0; i < XG_LAN_MAX_PEERS; i++) peers[i] = htonl(0xc0a80520 + (uint32_t)i);
    peer_count = XG_LAN_MAX_PEERS * 2; /* Defensive capacity clamp if a provider reports total count. */
    assert(send_packet() == 11 && send_count == XG_LAN_MAX_PEERS + 1);
    close(udp); close(tcp);
}

static void add_interface(const char *address, unsigned flags)
{
    int index = interface_count++;
    assert(index < 8);
    local_addresses[index] = (struct sockaddr_in){.sin_family = AF_INET, .sin_addr.s_addr = ip(address)};
    interfaces[index] = (struct ifaddrs){.ifa_addr = (struct sockaddr *)&local_addresses[index], .ifa_flags = flags};
    if (index) interfaces[index - 1].ifa_next = &interfaces[index];
}
static void test_interface_selection(void)
{
    unsigned lan = IFF_UP | IFF_RUNNING | IFF_BROADCAST;
    add_interface("169.254.10.2", lan);
    add_interface("192.168.5.2", lan);
    add_interface("10.0.0.2", lan);
    routed = ip("192.168.5.2");
    assert(xh_hostposix_local_ipv4_address() == routed); /* Valid Wi-Fi, despite USB first. */
    routed = ip("10.0.0.2");
    assert(xh_hostposix_local_ipv4_address() == routed); /* Existing Ethernet route wins. */
    routed = ip("100.64.1.2");
    assert(xh_hostposix_local_ipv4_address() == ip("192.168.5.2")); /* Cellular. */
    routed = ip("169.254.10.2");
    assert(xh_hostposix_local_ipv4_address() == ip("192.168.5.2"));
    interfaces[1].ifa_flags &= ~IFF_RUNNING;
    interfaces[2].ifa_flags |= IFF_POINTOPOINT;
    routed = ip("10.0.0.2");
    assert(xh_hostposix_local_ipv4_address() == ip("169.254.10.2")); /* Only USB LAN remains. */
    interfaces[0].ifa_flags |= IFF_LOOPBACK;
    assert(xh_hostposix_local_ipv4_address() == routed); /* No LAN: upstream fallback retained. */
    interface_count = 0;
    add_interface("127.0.0.2", lan); add_interface("224.0.0.1", lan); add_interface("0.0.0.0", lan);
    assert(xh_hostposix_local_ipv4_address() == routed);
    interface_count = 0; routed = 0;
    assert(xh_hostposix_local_ipv4_address() == 0);
    interface_failure = 1; routed = ip("10.1.1.2");
    int previous_frees = interface_frees;
    assert(xh_hostposix_local_ipv4_address() == routed && interface_frees == previous_frees);
}

int main(void)
{
    xg_base = (uintptr_t)memory;
    test_send_boundary();
    test_interface_selection();
    return 0;
}
