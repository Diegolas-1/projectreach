/*
 * xg_posix.c: the guest's posix_* imports (upstream's port/linux/src/posix.h).
 *
 * The implementations are upstream's own posix_*.c, compiled for the host
 * from the pinned checkout. These wrappers convert guest pointers, keep the
 * host errno for host_errno(), and convert socket addresses: the guest
 * passes Winsock/Linux layouts (a 16-bit family first), Darwin's start with
 * a length byte.
 */
#include "xg_host.h"
#include "xg_engine_compat.h"

#include <errno.h>
#include <netinet/in.h>
#include <string.h>
#include <sys/socket.h>

typedef int posix_long;
typedef unsigned int posix_ulong;
struct posix_file_information;

int posix_stat(const char *, struct posix_file_information *);
int posix_fstat(int, struct posix_file_information *);
int posix_set_file_times(const char *, posix_ulong, posix_ulong, posix_ulong, posix_ulong);
int posix_seek(int, posix_long, posix_long, int, posix_ulong *, posix_ulong *);
int posix_truncate(int, posix_ulong, posix_ulong);
int posix_disk_space(const char *, posix_ulong *, posix_ulong *, posix_ulong *, posix_ulong *);
int posix_set_read_only(const char *, int);
int posix_make_directory(const char *);
void *posix_directory_open(const char *);
int posix_directory_next(void *, char *, posix_ulong);
void posix_directory_close(void *);
int posix_find_entry_case_insensitive(const char *, const char *, char *, posix_ulong);
int posix_socket_last_error(void);
int posix_socket(int, int, int);
int posix_socket_close(int);
int posix_socket_bind(int, const void *, int);
int posix_socket_connect(int, const void *, int);
int posix_socket_listen(int, int);
int posix_socket_accept(int, void *, int *);
int posix_socket_send(int, const void *, int, int);
int posix_socket_sendto(int, const void *, int, int, const void *, int);
int posix_socket_recv(int, void *, int, int);
int posix_socket_recvfrom(int, void *, int, int, void *, int *);
int posix_socket_shutdown(int, int);
int posix_socket_set_nonblocking(int, int);
int posix_socket_bytes_available(int, posix_ulong *);
int posix_socket_set_nodelay(int);
int posix_socket_setsockopt(int, int, int, const void *, int);
int posix_socket_getsockopt(int, int, int, void *, int *);
int posix_socket_getsockname(int, void *, int *);
int posix_socket_getpeername(int, void *, int *);
int posix_socket_select(int *, int *, int *, int *, int *, int *, posix_long, posix_long, int);
posix_ulong posix_local_ipv4_address(void);
void posix_random_bytes(void *, posix_ulong);
posix_ulong posix_resolve_ipv4(const char *);
int posix_command_line_argument(int, char *, posix_ulong);
posix_ulong posix_process_id(void);
int posix_register_url_scheme(const char *, const char *);
int posix_discord_connect(void);
int posix_discord_write(int, const void *, int);
int posix_discord_read(int, void *, int);
void posix_discord_close(int);

#define KEEP(expression) do { __typeof__(expression) kept_ = (expression); xg_errno = xg_linux_errno(errno); return kept_; } while (0)

/* ---------- files */

int xh_hostposix_stat(uint32_t path, uint32_t info) { KEEP(posix_stat(G(const char *, path), GP(info))); }
int xh_hostposix_fstat(int fd, uint32_t info) { KEEP(posix_fstat(fd, GP(info))); }
int xh_hostposix_set_file_times(uint32_t path, posix_ulong a, posix_ulong b, posix_ulong c, posix_ulong d)
{ KEEP(posix_set_file_times(G(const char *, path), a, b, c, d)); }
int xh_hostposix_seek(int fd, posix_long low, posix_long high, int whence, uint32_t out_low, uint32_t out_high)
{ KEEP(posix_seek(fd, low, high, whence, GP(out_low), GP(out_high))); }
int xh_hostposix_truncate(int fd, posix_ulong low, posix_ulong high) { KEEP(posix_truncate(fd, low, high)); }
int xh_hostposix_disk_space(uint32_t path, uint32_t a, uint32_t b, uint32_t c, uint32_t d)
{ KEEP(posix_disk_space(G(const char *, path), GP(a), GP(b), GP(c), GP(d))); }
int xh_hostposix_set_read_only(uint32_t path, int value) { KEEP(posix_set_read_only(G(const char *, path), value)); }
int xh_hostposix_make_directory(uint32_t path) { KEEP(posix_make_directory(G(const char *, path))); }

/* directory streams are host pointers: the guest gets handles */
#define STREAMS 64
static void *streams[STREAMS];

uint32_t xh_hostposix_directory_open(uint32_t path)
{
	void *stream = posix_directory_open(G(const char *, path));
	uint32_t index;
	xg_errno = xg_linux_errno(errno);
	if (!stream)
		return 0;
	for (index = 1; index < STREAMS; index++)
		if (!streams[index])
		{
			streams[index] = stream;
			return index;
		}
	posix_directory_close(stream);
	xg_errno = 24;
	return 0;
}

int xh_hostposix_directory_next(uint32_t handle, uint32_t name, posix_ulong size)
{
	if (handle >= STREAMS || !streams[handle])
		return 0;
	KEEP(posix_directory_next(streams[handle], G(char *, name), size));
}

void xh_hostposix_directory_close(uint32_t handle)
{
	if (handle < STREAMS && streams[handle])
	{
		posix_directory_close(streams[handle]);
		streams[handle] = NULL;
	}
}

int xh_hostposix_find_entry_case_insensitive(uint32_t directory, uint32_t name, uint32_t out, posix_ulong size)
{ KEEP(posix_find_entry_case_insensitive(G(const char *, directory), G(const char *, name), G(char *, out), size)); }

/* ---------- sockets */

static socklen_t address_in(uint32_t guest, int length, struct sockaddr_storage *host)
{
	const uint8_t *bytes = G(const uint8_t *, guest);
	uint16_t family;
	if (!guest || length < 2 || length > (int)sizeof(*host))
		return 0;
	memcpy(host, bytes, (size_t)length);
	family = (uint16_t)(bytes[0] | bytes[1] << 8);
	host->ss_family = (sa_family_t)(family == 10 ? AF_INET6 : family);
	host->ss_len = (uint8_t)length;
	return (socklen_t)length;
}

static void address_out(const struct sockaddr_storage *host, socklen_t length, uint32_t guest, uint32_t guest_length)
{
	uint8_t *bytes;
	int capacity, family;
	if (!guest || !guest_length)
		return;
	bytes = G(uint8_t *, guest);
	capacity = *G(int *, guest_length);
	if ((int)length > capacity)
		length = (socklen_t)capacity;
	memcpy(bytes, host, length);
	family = host->ss_family == AF_INET6 ? 10 : host->ss_family;
	if (length >= 2)
	{
		bytes[0] = (uint8_t)family;
		bytes[1] = (uint8_t)(family >> 8);
	}
	*G(int *, guest_length) = (int)length;
}

int xh_hostposix_socket_last_error(void) { return posix_socket_last_error(); }
int xh_hostposix_socket(int family, int type, int protocol) { KEEP(posix_socket(family == 10 ? AF_INET6 : family, type, protocol)); }
int xh_hostposix_socket_close(int s) { KEEP(posix_socket_close(s)); }

int xh_hostposix_socket_bind(int s, uint32_t address, int length)
{
	struct sockaddr_storage host;
	socklen_t size = address_in(address, length, &host);
	KEEP(posix_socket_bind(s, size ? (void *)&host : NULL, (int)size));
}

int xh_hostposix_socket_connect(int s, uint32_t address, int length)
{
	struct sockaddr_storage host;
	socklen_t size = address_in(address, length, &host);
	KEEP(posix_socket_connect(s, size ? (void *)&host : NULL, (int)size));
}

int xh_hostposix_socket_listen(int s, int backlog) { KEEP(posix_socket_listen(s, backlog)); }

int xh_hostposix_socket_accept(int s, uint32_t address, uint32_t length)
{
	struct sockaddr_storage host;
	int size = sizeof(host), result = posix_socket_accept(s, &host, &size);
	xg_errno = xg_linux_errno(errno);
	if (result >= 0)
		address_out(&host, (socklen_t)size, address, length);
	return result;
}

int xh_hostposix_socket_send(int s, uint32_t buffer, int length, int flags) { KEEP(posix_socket_send(s, G(const void *, buffer), length, flags)); }

int xh_hostposix_socket_sendto(int s, uint32_t buffer, int length, int flags, uint32_t address, int address_length)
{
	struct sockaddr_storage host;
	socklen_t size = address_in(address, address_length, &host);
	KEEP(posix_socket_sendto(s, G(const void *, buffer), length, flags, size ? (void *)&host : NULL, (int)size));
}

int xh_hostposix_socket_recv(int s, uint32_t buffer, int length, int flags) { KEEP(posix_socket_recv(s, G(void *, buffer), length, flags)); }

int xh_hostposix_socket_recvfrom(int s, uint32_t buffer, int length, int flags, uint32_t address, uint32_t address_length)
{
	struct sockaddr_storage host;
	int size = sizeof(host), result = posix_socket_recvfrom(s, G(void *, buffer), length, flags, &host, &size);
	xg_errno = xg_linux_errno(errno);
	if (result >= 0)
		address_out(&host, (socklen_t)size, address, address_length);
	return result;
}

int xh_hostposix_socket_shutdown(int s, int how) { KEEP(posix_socket_shutdown(s, how)); }
int xh_hostposix_socket_set_nonblocking(int s, int value) { KEEP(posix_socket_set_nonblocking(s, value)); }
int xh_hostposix_socket_bytes_available(int s, uint32_t count) { KEEP(posix_socket_bytes_available(s, GP(count))); }
int xh_hostposix_socket_set_nodelay(int s) { KEEP(posix_socket_set_nodelay(s)); }
int xh_hostposix_socket_setsockopt(int s, int level, int name, uint32_t value, int length)
{ KEEP(posix_socket_setsockopt(s, level, name, GP(value), length)); }
int xh_hostposix_socket_getsockopt(int s, int level, int name, uint32_t value, uint32_t length)
{ KEEP(posix_socket_getsockopt(s, level, name, GP(value), GP(length))); }

int xh_hostposix_socket_getsockname(int s, uint32_t address, uint32_t length)
{
	struct sockaddr_storage host;
	int size = sizeof(host), result = posix_socket_getsockname(s, &host, &size);
	xg_errno = xg_linux_errno(errno);
	if (result == 0)
		address_out(&host, (socklen_t)size, address, length);
	return result;
}

int xh_hostposix_socket_getpeername(int s, uint32_t address, uint32_t length)
{
	struct sockaddr_storage host;
	int size = sizeof(host), result = posix_socket_getpeername(s, &host, &size);
	xg_errno = xg_linux_errno(errno);
	if (result == 0)
		address_out(&host, (socklen_t)size, address, length);
	return result;
}

int xh_hostposix_socket_select(uint32_t r, uint32_t rc, uint32_t w, uint32_t wc, uint32_t e, uint32_t ec,
	posix_long seconds, posix_long microseconds, int infinite)
{ KEEP(posix_socket_select(GP(r), GP(rc), GP(w), GP(wc), GP(e), GP(ec), seconds, microseconds, infinite)); }

posix_ulong xh_hostposix_local_ipv4_address(void) { return posix_local_ipv4_address(); }
void xh_hostposix_random_bytes(uint32_t buffer, posix_ulong size) { posix_random_bytes(G(void *, buffer), size); }
posix_ulong xh_hostposix_resolve_ipv4(uint32_t host) { return posix_resolve_ipv4(G(const char *, host)); }

/* ---------- UPnP: not wired up yet on Apple platforms */

int xh_hostposix_upnp_forward_udp(unsigned short port,
#if XG_UPNP_PREFERRED_PORT
    unsigned short preferred_port,
#endif
    uint32_t address, uint32_t external_port, uint32_t error, int size)
{
	(void)port; (void)address; (void)external_port;
#if XG_UPNP_PREFERRED_PORT
	(void)preferred_port;
#endif
	if (error && size > 0)
		strlcpy(G(char *, error), "UPnP is not available in HaloPad yet", (size_t)size);
	return 0;
}

void xh_hostposix_upnp_stop_forwarding_udp(unsigned short port) { (void)port; }

/* ---------- OpenCE 206 WebRTC transport (native Mbed TLS, guest buffers) */

#if XG_ENGINE_DTLS
int posix_dtls_fingerprint(unsigned char *);
int posix_dtls_open(void);
void posix_dtls_close(int);
void posix_dtls_input(int, const void *, int);
int posix_dtls_receive(int, posix_ulong, void *, int);
int posix_dtls_send(int, const void *, int);
int posix_dtls_output(int, void *, int);
int posix_dtls_peer_fingerprint(int, unsigned char *);
void posix_hmac_sha1(const void *, int, const void *, int, unsigned char *);

int xh_hostposix_dtls_fingerprint(uint32_t fingerprint)
{ return posix_dtls_fingerprint(GP(fingerprint)); }
int xh_hostposix_dtls_open(void) { return posix_dtls_open(); }
void xh_hostposix_dtls_close(int handle) { posix_dtls_close(handle); }
void xh_hostposix_dtls_input(int handle, uint32_t data, int size)
{ posix_dtls_input(handle, GP(data), size); }
int xh_hostposix_dtls_receive(int handle, posix_ulong now, uint32_t buffer, int size)
{ return posix_dtls_receive(handle, now, GP(buffer), size); }
int xh_hostposix_dtls_send(int handle, uint32_t data, int size)
{ return posix_dtls_send(handle, GP(data), size); }
int xh_hostposix_dtls_output(int handle, uint32_t buffer, int size)
{ return posix_dtls_output(handle, GP(buffer), size); }
int xh_hostposix_dtls_peer_fingerprint(int handle, uint32_t fingerprint)
{ return posix_dtls_peer_fingerprint(handle, GP(fingerprint)); }
void xh_hostposix_hmac_sha1(uint32_t key, int key_size, uint32_t data, int size, uint32_t digest)
{ posix_hmac_sha1(GP(key), key_size, GP(data), size, GP(digest)); }
#endif

/* ---------- the process and the desktop */

int xh_hostposix_command_line_argument(int index, uint32_t buffer, posix_ulong size)
{ return posix_command_line_argument(index, G(char *, buffer), size); }
posix_ulong xh_hostposix_process_id(void) { return posix_process_id(); }
int xh_hostposix_register_url_scheme(uint32_t scheme, uint32_t description)
{ (void)scheme; (void)description; return 0; }
/* Like upstream's Android host, there is no shared desktop-user secret. */
int xh_hostposix_user_secret(uint32_t secret, int size) { (void)secret; (void)size; return 0; }
int xh_hostposix_discord_connect(void) { return -1; }
int xh_hostposix_discord_write(int handle, uint32_t buffer, int length) { (void)handle; (void)buffer; (void)length; return -1; }
int xh_hostposix_discord_read(int handle, uint32_t buffer, int length) { (void)handle; (void)buffer; (void)length; return -1; }
void xh_hostposix_discord_close(int handle) { (void)handle; }
