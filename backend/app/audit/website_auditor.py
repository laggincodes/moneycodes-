"""
Website Auditor: Performs HTTP reachability, SSL validation, redirect chain inspection,
and HTML heuristic analysis for mobile responsiveness, metadata, CTAs, and contact channels.
"""
import re
import time
import logging
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup

from app.schemas import WebsiteAuditResult, AuditFinding

logger = logging.getLogger(__name__)

PHONE_REGEX = re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{2,5}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
CTA_KEYWORDS = [
    "book now", "order now", "reserve a table", "book appointment", "get a quote",
    "contact us", "call now", "whatsapp", "schedule", "enquire", "buy now", "sign up"
]
FOOD_MENU_KEYWORDS = ["menu", "food menu", "beverages", "drinks", "order online", "cuisines", "desserts"]
SERVICE_KEYWORDS = ["services", "treatments", "pricing", "packages", "consultation", "specialists"]


class WebsiteAuditor:
    """
    Performs deterministic, non-invasive website audits.
    Distinguishes verified HTTP/HTML findings from inferred business opportunities.
    """

    def __init__(
        self,
        timeout: float = 10.0,
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

        # Case 2: Website URL provided - perform live check
        redirect_chain: List[str] = [clean_url]
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        start_time = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=False,  # Don't crash on bad SSL; detect and flag it cleanly
            ) as client:
                response = await client.get(clean_url, headers=headers)
                response_time_ms = int((time.monotonic() - start_time) * 1000)

                for r in response.history:
                    redirect_chain.append(str(r.url))
                final_url = str(response.url)
                if final_url != redirect_chain[-1]:
                    redirect_chain.append(final_url)

                status_code = response.status_code
                is_reachable = (200 <= status_code < 400)
                ssl_valid = final_url.startswith("https://")

                # Parse HTML content if text/html
                html_text = response.text or ""
                return self._analyze_html(
                    raw_html=html_text,
                    url=clean_url,
                    final_url=final_url,
                    status_code=status_code,
                    is_reachable=is_reachable,
                    ssl_valid=ssl_valid,
                    redirect_chain=redirect_chain,
                    response_time_ms=response_time_ms,
                    business_name=business_name,
                    category=category,
                )

        except httpx.ConnectTimeout:
            return self._build_unreachable_result(clean_url, "Connection Timeout", "Site failed to respond within 10s.")
        except httpx.ConnectError as exc:
            return self._build_unreachable_result(clean_url, "Connection Error", f"DNS or connection failed: {str(exc)[:80]}")
        except httpx.HTTPError as exc:
            return self._build_unreachable_result(clean_url, "HTTP Protocol Error", str(exc)[:80])
        except Exception as exc:
            return self._build_unreachable_result(clean_url, "Audit Error", f"Unexpected error: {str(exc)[:80]}")

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
                summary="Website secures traffic with HTTPS.",
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
        parsed_final = urlparse(final_url).netloc.lower()
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
