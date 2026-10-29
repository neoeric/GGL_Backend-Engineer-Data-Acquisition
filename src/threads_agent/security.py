"""Exact URL scope and one budget shared by HTTP, meta and JS navigation."""
import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .core import Failure

HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
POST = re.compile(r"^/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)/?$")
SHARE = re.compile(r"^/share/([A-Za-z0-9_-]+)/?$")
PROFILE = re.compile(r"^/@([A-Za-z0-9._]+)/?$")


def check_url(url, profile=None, resource=False):
    try:
        u = urlsplit(url)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            raise ValueError()
        host = u.hostname
        if not host or url.count("\\") or any(ord(c) < 32 for c in url):
            raise ValueError()
        if resource:
            if host not in HOSTS and not host.endswith((".cdninstagram.com", ".fbcdn.net")):
                raise ValueError()
        elif host not in HOSTS:
            raise ValueError()
        elif profile and not (PROFILE.fullmatch(u.path) and PROFILE.fullmatch(u.path)[1] == profile):
            raise ValueError()
        elif not profile and not (POST.fullmatch(u.path) or SHARE.fullmatch(u.path)):
            raise ValueError()
        return urlunsplit((u.scheme, u.netloc.lower(), u.path, u.query, ""))
    except (ValueError, TypeError):
        raise Failure("unsupported_url", "Only HTTPS official Threads post/share URLs are accepted.", invalid=True)


async def public_dns(url):
    host = urlsplit(url).hostname
    try:
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), 4)
    except (OSError, TimeoutError):
        raise Failure("dns_error", technical=True)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise Failure("redirect_target_blocked", "DNS resolves to a non-public address.")


def post_identity(url):
    if not url:
        return None
    m = POST.fullmatch(urlsplit(url).path)
    return (m[1], m[2]) if m else None


class NavigationGuard:
    def __init__(self, source, profile=None, seconds=30):
        self.profile = profile
        self.current = check_url(source, profile)
        self.seen = {self.current}
        self.chain = [{"url": self.current, "kind": "initial"}]
        self.deadline = time.monotonic() + min(seconds, 30)
        self.hops = 0

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Failure("resolution_timeout")
        return left

    def follow(self, location, kind):
        self.remaining()
        target = urljoin(self.current, location)
        try:
            target = check_url(target, self.profile)
        except Failure:
            raise Failure("redirect_target_blocked", "Redirect/navigation leaves the allowed target scope.")
        if target in self.seen:
            raise Failure("redirect_loop")
        if self.hops >= 5:
            raise Failure("redirect_limit_exceeded")
        self.hops += 1
        self.seen.add(target)
        self.chain.append({"url": target, "kind": kind})
        self.current = target
        return target
