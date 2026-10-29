"""
Website Auditor: Performs hardened HTTP reachability, SSL validation, redirect inspection,
and HTML heuristic analysis for mobile responsiveness, metadata, CTAs, and contact channels.
Includes strict Server-Side Request Forgery (SSRF) and DNS rebinding protections.
"""
import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
from typing import Dict, List, Optional, Set, Tuple, Union
import urllib.parse

from bs4 import BeautifulSoup
import httpcore
import httpx

from app.schemas import AuditFinding, WebsiteAuditResult

logger = logging.getLogger(__name__)

# --- SSRF Protection Constants ---

ALLOWED_SCHEMES: Set[str] = {"http", "https"}
ALLOWED_PORTS: Set[int] = {80, 443, 8080, 8443}
MAX_REDIRECTS: int = 5
MAX_RESPONSE_BYTES: int = 512 * 1024  # 512 KB maximum response buffering
DEFAULT_CONNECT_TIMEOUT: float = 4.0
DEFAULT_READ_TIMEOUT: float = 6.0
DEFAULT_TOTAL_TIMEOUT: float = 10.0

# Comprehensive prohibited IP networks (IPv4 & IPv6)
PROHIBITED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918 private
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local / AWS/GCP/Azure Cloud Metadata
    ipaddress.ip_network("100.64.0.0/10"),    # Carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),        # Current network
    ipaddress.ip_network("240.0.0.0/4"),      # Reserved
    ipaddress.ip_network("224.0.0.0/4"),      # Multicast
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1 documentation
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2 documentation
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3 documentation
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique local (private)
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
    ipaddress.ip_network("ff00::/8"),         # IPv6 multicast
    ipaddress.ip_network("::/128"),           # IPv6 unspecified
    ipaddress.ip_network("2001:db8::/32"),    # IPv6 documentation
]

# Blacklisted hostnames and metadata endpoints
BLOCKED_HOSTNAMES: Set[str] = {
    "localhost",
    "metadata.google.internal",
    "instance-data",
    "metadata.packet.net",
}

BLOCKED_HOSTNAME_SUFFIXES: Tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".corp",
    ".lan",
    ".home",
    ".onion",
)

PHONE_REGEX = re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{2,5}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
CTA_KEYWORDS = [
    "book now", "order now", "reserve a table", "book appointment", "get a quote",
    "contact us", "call now", "whatsapp", "schedule", "enquire", "buy now", "sign up"
]
FOOD_MENU_KEYWORDS = ["menu", "food menu", "beverages", "drinks", "order online", "cuisines", "desserts"]
SERVICE_KEYWORDS = ["services", "treatments", "pricing", "packages", "consultation", "specialists"]


class SSRFBlockedError(Exception):
    """Raised when a request is blocked by SSRF prevention policies."""
    pass


def is_ip_prohibited(ip_obj: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> Tuple[bool, str]:
    """
    Checks if an IP address belongs to loopback, private, link-local,
    cloud metadata, multicast, unspecified, or reserved networks.
    Unwraps IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1).
    """
    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 -> 127.0.0.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        ip_obj = ip_obj.ipv4_mapped

    if ip_obj.is_loopback:
        return True, f"Loopback address ({ip_obj}) is prohibited"
    if ip_obj.is_link_local:
        return True, f"Link-local / cloud metadata address ({ip_obj}) is prohibited"
    if ip_obj.is_unspecified:
        return True, f"Unspecified address ({ip_obj}) is prohibited"
    if ip_obj.is_multicast:
        return True, f"Multicast address ({ip_obj}) is prohibited"
    if ip_obj.is_private:
        return True, f"Private network address ({ip_obj}) is prohibited"
    if ip_obj.is_reserved:
        return True, f"Reserved network address ({ip_obj}) is prohibited"

    for net in PROHIBITED_NETWORKS:
        if ip_obj in net:
            return True, f"Address ({ip_obj}) belongs to restricted subnet {net}"

    return False, ""


def parse_raw_ip(host_str: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """Attempts to parse standard, decimal integer, hex, or BSD shorthand IP representations."""
    clean = host_str.strip()
    try:
        return ipaddress.ip_address(clean)
    except ValueError:
        pass

    # Check integer decimal literal (e.g. 2130706433)
    if clean.isdigit():
        try:
            return ipaddress.ip_address(int(clean))
        except ValueError:
            pass

    # Check hex literal (e.g. 0x7f000001)
    if clean.startswith(("0x", "0X")):
        try:
            return ipaddress.ip_address(int(clean, 16))
        except ValueError:
            pass

    # Check shorthand BSD dot-notation (e.g. 127.1)
    if "." in clean and all(part.isdigit() for part in clean.split(".")):
        try:
            raw_bytes = socket.inet_aton(clean)
            return ipaddress.ip_address(raw_bytes)
        except Exception:
            pass

    return None


def validate_url(url: str) -> urllib.parse.SplitResult:
    """
    Validates a URL before any network connection:
    - Scheme must be strictly http or https
    - Rejects embedded credentials (user:pass@)
    - Rejects prohibited ports (only standard web ports allowed)
    - Rejects localhost aliases, metadata names, and integer/hex IP literals
    """
    if not url or not isinstance(url, str):
        raise SSRFBlockedError("URL must be a non-empty string.")

    cleaned_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except Exception as exc:
        raise SSRFBlockedError(f"Malformed URL: {exc}")

    # 1. Scheme Check
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"Scheme '{scheme}' is prohibited. Only HTTP and HTTPS are permitted.")

    # 2. Embedded Credentials Check
    if parsed.username or parsed.password or ("@" in parsed.netloc):
        raise SSRFBlockedError("URLs with embedded credentials (user:pass@) are prohibited.")

    # 3. Hostname Check
    host = parsed.hostname
    if not host or not host.strip():
        raise SSRFBlockedError("URL has an empty hostname.")

    clean_host = host.strip().lower()

    # Reject null bytes or whitespace
    if any(c in clean_host for c in ["\x00", " ", "\t", "\r", "\n"]):
        raise SSRFBlockedError("Hostname contains invalid control characters or whitespace.")

    # Check blocked hostnames and suffixes
    if clean_host in BLOCKED_HOSTNAMES:
        raise SSRFBlockedError(f"Target host '{clean_host}' is prohibited by security policy.")

    for suffix in BLOCKED_HOSTNAME_SUFFIXES:
        if clean_host.endswith(suffix):
            raise SSRFBlockedError(f"Target host '{clean_host}' belongs to prohibited local domain.")

    # 4. Check for direct, decimal integer, hex, or shorthand IP targets
    raw_ip = parse_raw_ip(clean_host)
    if raw_ip is not None:
        is_bad, reason = is_ip_prohibited(raw_ip)
        if is_bad:
            raise SSRFBlockedError(f"IP target is prohibited: {reason}")

    # 5. Port Check
    port = parsed.port
    if port is not None and port not in ALLOWED_PORTS:
        raise SSRFBlockedError(f"Target port {port} is prohibited. Allowed web ports: {sorted(ALLOWED_PORTS)}")

    return parsed


class SSRFSafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """
    Custom httpcore network backend that intercepts connect_tcp calls:
    - Resolves DNS asynchronously
    - Validates ALL resolved IP addresses (prevents mixed answers)
    - Directly connects the underlying TCP socket to the validated IP (prevents DNS rebinding / TOCTOU)
    - Preserves original hostname for SNI and TLS certificate verification
    """

    def __init__(self):
        self._inner = httpcore._backends.auto.AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        clean_host = host.strip().strip("[]")

        # Resolve DNS addresses
        loop = asyncio.get_running_loop()
        try:
            addr_info = await loop.getaddrinfo(
                clean_host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise httpcore.ConnectError(f"DNS resolution failed for '{clean_host}': {exc}") from exc

        if not addr_info:
            raise httpcore.ConnectError(f"No DNS records found for host '{clean_host}'")

        validated_ips: List[str] = []
        # Crucial security rule: validate ALL returned IP addresses.
        # If ANY returned address is prohibited, reject the entire host!
        for entry in addr_info:
            sockaddr = entry[4]
            ip_str = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                raise SSRFBlockedError(f"Unparseable IP address '{ip_str}' returned by DNS for '{clean_host}'")

            is_bad, reason = is_ip_prohibited(ip_obj)
            if is_bad:
                raise SSRFBlockedError(
                    f"Host '{clean_host}' resolved to restricted destination '{ip_str}': {reason}"
                )
            if ip_str not in validated_ips:
                validated_ips.append(ip_str)

        if not validated_ips:
            raise SSRFBlockedError(f"No valid public IP addresses available for host '{clean_host}'")

        # Pick the first validated IP address to pin the connection directly
        target_ip = validated_ips[0]

        # Connect directly to the validated IP address
        # Note: httpcore passes server_hostname to stream.start_tls() separately,
        # preserving the original host for SNI and TLS certificate verification!
        return await self._inner.connect_tcp(
            target_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout: Optional[float] = None, socket_options=None):
        raise SSRFBlockedError("Unix domain socket connections are prohibited.")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def create_ssrf_safe_client(timeout: float = DEFAULT_TOTAL_TIMEOUT) -> httpx.AsyncClient:
    """
    Constructs an httpx.AsyncClient equipped with SSRFSafeNetworkBackend,
    enforcing TLS certificate verification and safe timeouts.
    """
    safe_backend = SSRFSafeNetworkBackend()
    pool = httpcore.AsyncConnectionPool(
        network_backend=safe_backend,
        ssl_context=None,  # Standard system SSL context with verification
        max_connections=15,
        max_keepalive_connections=5,
        keepalive_expiry=5.0,
        retries=0,
    )
    transport = httpx.AsyncHTTPTransport(
        verify=True,  # STRICT TLS certificate verification enabled
        trust_env=False,  # Prevent ambient proxy environment variables from bypassing validation
    )
    transport._pool = pool

    timeouts = httpx.Timeout(
        timeout=timeout,
        connect=DEFAULT_CONNECT_TIMEOUT,
        read=DEFAULT_READ_TIMEOUT,
        write=DEFAULT_CONNECT_TIMEOUT,
        pool=DEFAULT_CONNECT_TIMEOUT,
    )

    return httpx.AsyncClient(
        transport=transport,
        timeout=timeouts,
        follow_redirects=False,  # Manual redirect validation
        headers={"Accept-Encoding": "gzip, deflate"},
    )


class WebsiteAuditor:
    """
    Performs deterministic, non-invasive website audits with strict SSRF defense.
    Distinguishes verified findings from inferred opportunities.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TOTAL_TIMEOUT,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 WebsiteAuditor/1.0"
        ),
    ):
        self.timeout = timeout
        self.user_agent = user_agent

    async def audit(
        self,
        url: Optional[str],
        business_name: str,
        category: str = "Local Business",
    ) -> WebsiteAuditResult:
        """
        Audits a website if URL is provided, or generates a no-website assessment.
        All requests are executed within the SSRF-hardened boundary.
        """
        # Case 1: No website URL listed
        if not url or not url.strip():
            return WebsiteAuditResult(
                has_website_url=False,
                url=None,
                final_url=None,
                status_code=None,
                is_reachable=False,
                redirect_count=0,
                redirect_chain=[],
                response_time_ms=None,
                ssl_valid=False,
                has_title=False,
                has_viewport_meta=False,
                has_description_meta=False,
                has_menu=False,
                has_contact_phone=False,
                has_contact_email=False,
                has_address_text=False,
                has_clear_cta=False,
                has_social_links=False,
                detected_socials=[],
                mobile_friendly_indicator=False,
                mobile_issues=["No website detected in public records"],
                findings=[
                    AuditFinding(
                        check="Website Presence",
                        status="verified",
                        passed=False,
                        summary="No official website URL listed in public business registries or OpenStreetMap.",
                        evidence="Empty website field in source listing.",
                    ),
                    AuditFinding(
                        check="Digital Opportunity",
                        status="inferred",
                        passed=False,
                        summary="High potential client: business is operating without a dedicated standalone website or domain.",
                        evidence="Missing digital storefront impairs local SEO and direct customer acquisition.",
                    ),
                ],
            )

        clean_url = url.strip()
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            clean_url = f"https://{clean_url}"

        # Pre-validate starting URL
        try:
            validate_url(clean_url)
        except SSRFBlockedError as exc:
            logger.warning(f"Initial audit URL blocked by SSRF policy: {exc}")
            return self._build_blocked_result(clean_url, str(exc))

        redirect_chain: List[str] = [clean_url]
        current_url = clean_url
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        start_time = time.monotonic()
        hop_count = 0
        response_data: Optional[httpx.Response] = None
        html_text = ""
        ssl_error_detected = False

        try:
            async with create_ssrf_safe_client(timeout=self.timeout) as client:
                while hop_count < MAX_REDIRECTS:
                    try:
                        # Stream response to cap maximum buffered size
                        async with client.stream("GET", current_url, headers=headers) as response:
                            response_data = response
                            status_code = response.status_code

                            # Check for redirects (3xx)
                            if 300 <= status_code < 400:
                                hop_count += 1
                                location = response.headers.get("Location")
                                if not location:
                                    break

                                next_url = urllib.parse.urljoin(current_url, location)

                                # Check redirect loops
                                if next_url in redirect_chain:
                                    raise SSRFBlockedError(f"Redirect loop detected targeting {next_url}")

                                # Re-validate destination URL
                                validate_url(next_url)

                                redirect_chain.append(next_url)
                                current_url = next_url
                                continue

                            # Reached final non-redirect response
                            # Verify content-type is acceptable (HTML or text)
                            content_type = response.headers.get("Content-Type", "").lower()
                            if content_type and not any(ct in content_type for ct in ["text/html", "application/xhtml", "text/plain", "application/xml"]):
                                # Non-HTML response (binary, PDF, etc.) - do not parse
                                html_text = ""
                                break

                            # Read response body with maximum byte cap
                            chunks = []
                            total_bytes = 0
                            async for chunk in response.aiter_bytes(chunk_size=8192):
                                total_bytes += len(chunk)
                                chunks.append(chunk)
                                if total_bytes >= MAX_RESPONSE_BYTES:
                                    logger.info(f"Response size reached limit ({MAX_RESPONSE_BYTES} bytes); truncating safely.")
                                    break

                            raw_bytes = b"".join(chunks)
                            encoding = response.encoding or "utf-8"
                            html_text = raw_bytes.decode(encoding, errors="replace")
                            break

                    except (ssl.SSLCertVerificationError, httpx.ConnectError) as exc:
                        err_str = str(exc)
                        if "certificate" in err_str.lower() or "ssl" in err_str.lower():
                            ssl_error_detected = True
                            return self._build_ssl_failure_result(current_url, err_str)
                        raise

                if hop_count >= MAX_REDIRECTS and response_data and (300 <= response_data.status_code < 400):
                    raise SSRFBlockedError(f"Exceeded maximum redirect limit ({MAX_REDIRECTS} hops).")

            response_time_ms = int((time.monotonic() - start_time) * 1000)
            final_url = redirect_chain[-1]
            final_status = response_data.status_code if response_data else 200
            is_reachable = (200 <= final_status < 400)
            ssl_valid = final_url.startswith("https://") and not ssl_error_detected

            return self._analyze_html(
                raw_html=html_text,
                url=clean_url,
                final_url=final_url,
                status_code=final_status,
                is_reachable=is_reachable,
                ssl_valid=ssl_valid,
                redirect_chain=redirect_chain,
                response_time_ms=response_time_ms,
                business_name=business_name,
                category=category,
            )

        except SSRFBlockedError as exc:
            return self._build_blocked_result(clean_url, str(exc))
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException):
            return self._build_unreachable_result(clean_url, "Connection Timeout", "Site failed to respond within timeout window.")
        except httpx.ConnectError as exc:
            return self._build_unreachable_result(clean_url, "Connection Error", f"DNS or TCP connection failed: {str(exc)[:80]}")
        except httpx.HTTPError as exc:
            return self._build_unreachable_result(clean_url, "HTTP Error", str(exc)[:80])
        except Exception as exc:
            logger.exception(f"Unexpected audit exception for {clean_url}: {exc}")
            return self._build_unreachable_result(clean_url, "Audit Error", f"Unexpected error: {str(exc)[:80]}")

    def _build_blocked_result(self, url: str, reason: str) -> WebsiteAuditResult:
        """Returns structured audit result when an audit is blocked by SSRF policy."""
        return WebsiteAuditResult(
            has_website_url=True,
            url=url,
            final_url=url,
            status_code=None,
            is_reachable=False,
            redirect_count=0,
            redirect_chain=[url],
            response_time_ms=None,
            ssl_valid=False,
            has_title=False,
            has_viewport_meta=False,
            has_description_meta=False,
            has_menu=False,
            has_contact_phone=False,
            has_contact_email=False,
            has_address_text=False,
            has_clear_cta=False,
            has_social_links=False,
            detected_socials=[],
            mobile_friendly_indicator=False,
            mobile_issues=["Audit restricted by safety policy"],
            findings=[
                AuditFinding(
                    check="Security Policy",
                    status="verified",
                    passed=False,
                    summary="Audit target blocked by security policy: Destination violates networking policy.",
                    evidence=reason,
                ),
                AuditFinding(
                    check="Reachability",
                    status="inferred",
                    passed=False,
                    summary="External audit prevented because target points to a private, loopback, or metadata destination.",
                    evidence=reason,
                ),
            ],
        )

    def _build_ssl_failure_result(self, url: str, detail: str) -> WebsiteAuditResult:
        """Returns structured audit result when TLS certificate validation fails."""
        return WebsiteAuditResult(
            has_website_url=True,
            url=url,
            final_url=url,
            status_code=None,
            is_reachable=False,
            redirect_count=0,
            redirect_chain=[url],
            response_time_ms=None,
            ssl_valid=False,
            has_title=False,
            has_viewport_meta=False,
            has_description_meta=False,
            has_menu=False,
            has_contact_phone=False,
            has_contact_email=False,
            has_address_text=False,
            has_clear_cta=False,
            has_social_links=False,
            detected_socials=[],
            mobile_friendly_indicator=False,
            mobile_issues=["Invalid SSL Certificate"],
            findings=[
                AuditFinding(
                    check="SSL Security",
                    status="verified",
                    passed=False,
                    summary="SSL certificate verification failed (self-signed, expired, or untrusted).",
                    evidence=detail[:100],
                ),
                AuditFinding(
                    check="Browser Security Warning",
                    status="inferred",
                    passed=False,
                    summary="Prospective customers receive security warnings when visiting this domain.",
                    evidence="Untrusted SSL Certificate",
                ),
            ],
        )

    def _build_unreachable_result(self, url: str, error_type: str, detail: str) -> WebsiteAuditResult:
        return WebsiteAuditResult(
            has_website_url=True,
            url=url,
            final_url=url,
            status_code=None,
            is_reachable=False,
            redirect_count=0,
            redirect_chain=[url],
            response_time_ms=None,
            ssl_valid=False,
            has_title=False,
            has_viewport_meta=False,
            has_description_meta=False,
            has_menu=False,
            has_contact_phone=False,
            has_contact_email=False,
            has_address_text=False,
            has_clear_cta=False,
            has_social_links=False,
            detected_socials=[],
            mobile_friendly_indicator=False,
            mobile_issues=["Site is completely unreachable"],
            findings=[
                AuditFinding(
                    check="Reachability",
                    status="verified",
                    passed=False,
                    summary=f"Website is down or unreachable: {error_type}",
                    evidence=detail,
                ),
                AuditFinding(
                    check="Business Impact",
                    status="inferred",
                    passed=False,
                    summary="Potential customers attempting to visit the site face downtime or broken DNS.",
                    evidence="Immediate outreach opportunity to repair or re-host site.",
                ),
            ],
        )

    def _analyze_html(
        self,
        raw_html: str,
        url: str,
        final_url: str,
        status_code: int,
        is_reachable: bool,
        ssl_valid: bool,
        redirect_chain: List[str],
        response_time_ms: int,
        business_name: str,
        category: str,
    ) -> WebsiteAuditResult:
        soup = BeautifulSoup(raw_html[:300000], "html.parser")  # Parse first 300KB
        findings: List[AuditFinding] = []
        mobile_issues: List[str] = []

        # Reachability Finding
        if is_reachable:
            findings.append(AuditFinding(
                check="HTTP Status",
                status="verified",
                passed=True,
                summary=f"Website responded with HTTP status {status_code} in {response_time_ms}ms.",
                evidence=f"Final URL: {final_url}",
            ))
        else:
            findings.append(AuditFinding(
                check="HTTP Status",
                status="verified",
                passed=False,
                summary=f"Website returned error status HTTP {status_code}.",
                evidence=f"Returned error code {status_code}",
            ))

        # SSL Finding
        if ssl_valid:
            findings.append(AuditFinding(
                check="SSL Security",
                status="verified",
                passed=True,
                summary="Website secures traffic with valid HTTPS.",
                evidence=final_url,
            ))
        else:
            findings.append(AuditFinding(
                check="SSL Security",
                status="verified",
                passed=False,
                summary="Website lacks HTTPS encryption or redirects to insecure HTTP.",
                evidence="Insecure connection warning in browsers.",
            ))

        # Social redirect check (e.g. website is just a Facebook or Instagram page)
        parsed_final = urllib.parse.urlsplit(final_url).netloc.lower()
        if any(social in parsed_final for social in ["facebook.com", "instagram.com", "linktr.ee", "wa.me"]):
            findings.append(AuditFinding(
                check="Social Redirect",
                status="verified",
                passed=False,
                summary=f"Business URL redirects to a third-party platform ({parsed_final}) instead of a branded standalone domain.",
                evidence=final_url,
            ))

        # Page Title check
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else None
        has_title = bool(title_text and len(title_text) > 2)
        if has_title:
            is_generic = title_text.lower() in ["home", "homepage", "untitled", "index", "default"]
            if is_generic:
                findings.append(AuditFinding(
                    check="SEO Page Title",
                    status="verified",
                    passed=False,
                    summary=f"Page title is generic ('{title_text}') and lacks brand identity.",
                    evidence=f"<title>{title_text}</title>",
                ))
            else:
                findings.append(AuditFinding(
                    check="SEO Page Title",
                    status="verified",
                    passed=True,
                    summary=f"Page title configured: '{title_text[:60]}'.",
                    evidence=f"<title>{title_text[:60]}</title>",
                ))
        else:
            findings.append(AuditFinding(
                check="SEO Page Title",
                status="verified",
                passed=False,
                summary="Page is missing a `<title>` tag, hurting search rankings.",
                evidence="No <title> found in document head.",
            ))

        # Viewport Meta (Crucial for mobile responsiveness)
        viewport_meta = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        has_viewport = bool(viewport_meta and "width=device-width" in str(viewport_meta.get("content", "")).lower())
        if has_viewport:
            findings.append(AuditFinding(
                check="Mobile Viewport",
                status="verified",
                passed=True,
                summary="Mobile viewport meta tag is properly configured.",
                evidence=str(viewport_meta.get("content")),
            ))
        else:
            mobile_issues.append("Missing `<meta name='viewport' content='width=device-width'>`")
            findings.append(AuditFinding(
                check="Mobile Viewport",
                status="verified",
                passed=False,
                summary="Missing or misconfigured mobile viewport tag; pages may render as desktop shrink-to-fit on phones.",
                evidence="Viewport meta tag missing in <head>.",
            ))

        # Meta Description
        desc_meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        has_desc = bool(desc_meta and desc_meta.get("content", "").strip())
        if has_desc:
            findings.append(AuditFinding(
                check="Meta Description",
                status="verified",
                passed=True,
                summary="Meta description is present for search engine snippets.",
                evidence=str(desc_meta.get("content", ""))[:80],
            ))
        else:
            findings.append(AuditFinding(
                check="Meta Description",
                status="verified",
                passed=False,
                summary="Missing meta description tag, leading to auto-generated snippet in Google results.",
                evidence="No description tag found in <head>.",
            ))

        # Contact Details Heuristic
        body_text = soup.get_text(" ", strip=True)
        tel_links = soup.find_all("a", href=re.compile(r"^tel:", re.I))
        phone_matches = PHONE_REGEX.findall(body_text)
        has_contact_phone = bool(tel_links or phone_matches)

        mailto_links = soup.find_all("a", href=re.compile(r"^mailto:", re.I))
        email_matches = EMAIL_REGEX.findall(body_text)
        has_contact_email = bool(mailto_links or email_matches)

        if has_contact_phone or has_contact_email:
            evidence_str = []
            if tel_links:
                evidence_str.append("Click-to-call tel: links")
            elif phone_matches:
                evidence_str.append(f"Phone text detected: {phone_matches[0][:20]}")
            if mailto_links:
                evidence_str.append("mailto: link")
            findings.append(AuditFinding(
                check="Contact Information",
                status="verified",
                passed=True,
                summary="Found direct contact channels on the page.",
                evidence=", ".join(evidence_str),
            ))
        else:
            findings.append(AuditFinding(
                check="Contact Information",
                status="inferred",
                passed=False,
                summary="No obvious direct telephone or email links detected on homepage.",
                evidence="No tel:, mailto: or phone patterns detected.",
            ))

        # Menu / Services Heuristic
        has_menu = False
        lower_category = category.lower()
        if any(c in lower_category for c in ["caf", "restaurant", "food"]):
            has_menu = any(kw in body_text.lower() for kw in FOOD_MENU_KEYWORDS)
            findings.append(AuditFinding(
                check="Menu & Ordering",
                status="inferred",
                passed=has_menu,
                summary="Menu information detected on website." if has_menu else "No prominent online menu or dish listing detected for food establishment.",
                evidence="Menu keywords present in page text" if has_menu else "Missing menu / online ordering links",
            ))
        else:
            has_services = any(kw in body_text.lower() for kw in SERVICE_KEYWORDS)
            findings.append(AuditFinding(
                check="Service Catalog",
                status="inferred",
                passed=has_services,
                summary="Service listing or packages detected." if has_services else "No clear service packages or treatment breakdown found.",
                evidence="Service keywords present" if has_services else "No structured service catalog identified",
            ))

        # Call to Action (CTA) Heuristics
        buttons_and_links = [el.get_text(strip=True).lower() for el in soup.find_all(["button", "a"]) if el.get_text(strip=True)]
        found_ctas = [cta for cta in CTA_KEYWORDS if any(cta in text for text in buttons_and_links)]
        has_clear_cta = len(found_ctas) > 0
        if has_clear_cta:
            findings.append(AuditFinding(
                check="Call to Action",
                status="verified",
                passed=True,
                summary=f"Clear conversion action buttons detected ({', '.join(found_ctas[:3])}).",
                evidence=f"CTAs: {', '.join(found_ctas[:3])}",
            ))
        else:
            findings.append(AuditFinding(
                check="Call to Action",
                status="inferred",
                passed=False,
                summary="No prominent conversion action (e.g. 'Book Table', 'Call Now', 'Order Online', 'WhatsApp Us') detected.",
                evidence="Missing high-visibility conversion buttons",
            ))

        # Social Media Links
        social_domains = ["instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "wa.me"]
        detected_socials: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            for s_dom in social_domains:
                if s_dom in href and s_dom not in detected_socials:
                    detected_socials.append(s_dom)

        has_social_links = len(detected_socials) > 0

        # Mobile friendly heuristic indicator
        mobile_friendly_indicator = has_viewport and (len(mobile_issues) == 0)

        return WebsiteAuditResult(
            has_website_url=True,
            url=url,
            final_url=final_url,
            status_code=status_code,
            is_reachable=is_reachable,
            redirect_count=max(0, len(redirect_chain) - 1),
            redirect_chain=redirect_chain,
            response_time_ms=response_time_ms,
            ssl_valid=ssl_valid,
            has_title=has_title,
            title_text=title_text,
            has_viewport_meta=has_viewport,
            has_description_meta=has_desc,
            has_menu=has_menu,
            has_contact_phone=has_contact_phone,
            has_contact_email=has_contact_email,
            has_address_text=True if ("address" in body_text.lower() or "road" in body_text.lower()) else False,
            has_clear_cta=has_clear_cta,
            has_social_links=has_social_links,
            detected_socials=detected_socials,
            mobile_friendly_indicator=mobile_friendly_indicator,
            mobile_issues=mobile_issues,
            findings=findings,
        )
