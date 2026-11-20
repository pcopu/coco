/* Boundary safety guard, inherited by dynamically linked child executables.
 * Landlock separately restricts filesystem access and TCP destination ports.
 * This guard restricts addresses and signals to this test's process group.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <signal.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static int deny(const char *operation) {
    const char *path = getenv("COCO_E2E_DENIAL_LOG");
    if (path) {
        FILE *out = fopen(path, "a");
        if (out) { fprintf(out, "%s\n", operation); fclose(out); }
    }
    errno = EACCES;
    return -1;
}

static int address_allowed(const struct sockaddr *addr) {
    if (!addr) return 1;
    if (addr->sa_family == AF_INET)
        return ((const struct sockaddr_in *)addr)->sin_addr.s_addr == htonl(INADDR_LOOPBACK);
    if (addr->sa_family == AF_INET6)
        return IN6_IS_ADDR_LOOPBACK(&((const struct sockaddr_in6 *)addr)->sin6_addr);
    /* No access to local services over Unix domain sockets. socketpair is OK. */
    return 0;
}

static unsigned int address_port(const struct sockaddr *addr) {
    if (addr->sa_family == AF_INET) return ntohs(((const struct sockaddr_in *)addr)->sin_port);
    if (addr->sa_family == AF_INET6) return ntohs(((const struct sockaddr_in6 *)addr)->sin6_port);
    return 0;
}

static int port_allowed(unsigned int port) {
    const char *raw = getenv("COCO_E2E_PORTS");
    if (raw) {
        char *values = strdup(raw), *save = NULL;
        for (char *value = strtok_r(values, ",", &save); value; value = strtok_r(NULL, ",", &save)) {
            if ((unsigned int)atoi(value) == port) { free(values); return 1; }
        }
        free(values);
    }
    const char *path = getenv("COCO_E2E_DYNAMIC_PORTS");
    if (path) {
        FILE *stream = fopen(path, "r");
        unsigned int registered;
        if (stream) {
            while (fscanf(stream, "%u", &registered) == 1) {
                if (registered == port) { fclose(stream); return 1; }
            }
            fclose(stream);
        }
    }
    return 0;
}

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    if (!address_allowed(addr) || !port_allowed(address_port(addr))) return deny("connect");
    int (*real_fn)(int,const struct sockaddr *,socklen_t) = dlsym(RTLD_NEXT,"connect");
    int result = real_fn(fd,addr,len);
    return result;
}
int bind(int fd, const struct sockaddr *addr, socklen_t len) {
    if (!address_allowed(addr)) return deny("bind");
    int (*real_fn)(int,const struct sockaddr *,socklen_t) = dlsym(RTLD_NEXT,"bind");
    int result = real_fn(fd,addr,len);
    const char *path = getenv("COCO_E2E_DYNAMIC_PORTS");
    if (result == 0 && path) {
        struct sockaddr_storage bound;
        socklen_t size = sizeof(bound);
        if (getsockname(fd, (struct sockaddr *)&bound, &size) == 0) {
            FILE *stream = fopen(path, "a");
            if (stream) { fprintf(stream, "%u\n", address_port((struct sockaddr *)&bound)); fclose(stream); }
        }
    }
    return result;
}
ssize_t sendto(int fd, const void *buf, size_t size, int flags,
               const struct sockaddr *addr, socklen_t len) {
    if (addr && (!address_allowed(addr) || !port_allowed(address_port(addr)))) return deny("sendto");
    ssize_t (*real_fn)(int,const void *,size_t,int,const struct sockaddr *,socklen_t) = dlsym(RTLD_NEXT,"sendto");
    return real_fn(fd,buf,size,flags,addr,len);
}
int getaddrinfo(const char *node,const char *service,const struct addrinfo *hints,
                struct addrinfo **result) {
    struct in_addr v4;
    struct in6_addr v6;
    if (node && !(inet_pton(AF_INET,node,&v4)==1 && v4.s_addr==htonl(INADDR_LOOPBACK)) &&
        !(inet_pton(AF_INET6,node,&v6)==1 && IN6_IS_ADDR_LOOPBACK(&v6))) {
        deny("dns"); return EAI_FAIL;
    }
    int (*real_fn)(const char *,const char *,const struct addrinfo *,struct addrinfo **) = dlsym(RTLD_NEXT,"getaddrinfo");
    return real_fn(node,service,hints,result);
}
int kill(pid_t pid,int sig) {
    if (pid <= 0 || getpgid(pid) != getpgrp()) return deny("kill");
    int (*real_fn)(pid_t,int) = dlsym(RTLD_NEXT,"kill");
    return real_fn(pid,sig);
}
int killpg(pid_t group,int sig) {
    if (group != getpgrp()) return deny("killpg");
    int (*real_fn)(pid_t,int) = dlsym(RTLD_NEXT,"killpg");
    return real_fn(group,sig);
}
