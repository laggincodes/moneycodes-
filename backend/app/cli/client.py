"""
HTTP Client for WebsiteLead Hunter CLI.
Enforces bounded timeouts, Bearer authentication, server-side idempotency,
and strict redaction of sensitive credentials.
"""
import os
import json
import logging
from typing import Optional, Dict, Any, List
import httpx

from app.cli.credentials import load_session_token

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_default_transport = None


def set_default_transport(transport):
    """Sets a global transport for testing (e.g. httpx.ASGITransport(app=app))."""
    global _default_transport
    _default_transport = transport


class CLIError(Exception):
    """Base CLI Exception."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CLIAuthError(CLIError):
    """Authentication required or session expired (401)."""
    pass


class CLIPermissionError(CLIError):
    """Permission denied or CSRF/origin violation (403)."""
    pass


class CLINotFoundError(CLIError):
    """Resource not found (404)."""
    pass


class CLIValidationError(CLIError):
    """Request validation error (400)."""
    pass


class CLINetworkError(CLIError):
    """Network connection failure or timeout."""
    pass


class LeadHunterClient:
    """Synchronous HTTP client for interacting with WebsiteLead Hunter backend API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        clean_url = base_url.rstrip("/")
        if clean_url.endswith("/api"):
            clean_url = clean_url[:-4]
        self.base_url = clean_url
        self.timeout = timeout
        self.transport = transport if transport is not None else _default_transport

    def _get_auth_headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "WebsiteLeadHunter-CLI/1.0",
        }
        cred = load_session_token()
        if cred:
            _email, token = cred
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _handle_response(self, response: httpx.Response) -> Any:
        try:
            status = response.status_code
            if 200 <= status < 300:
                if response.headers.get("content-type", "").startswith("application/json"):
                    return response.json()
                return response.text

            # Parse error message safely without leaking sensitive information
            try:
                err_data = response.json()
                detail = err_data.get("detail", response.text)
            except Exception:
                detail = response.text or f"HTTP status {status}"

            if status == 401:
                raise CLIAuthError("Authentication required or session expired. Please run 'hunt login'.", status_code=status)
            elif status == 403:
                raise CLIPermissionError(f"Access forbidden: {detail}", status_code=status)
            elif status == 404:
                raise CLINotFoundError(f"Resource not found: {detail}", status_code=status)
            elif status == 400 or status == 422:
                raise CLIValidationError(f"Validation error: {detail}", status_code=status)
            else:
                raise CLIError(f"Server error ({status}): {detail}", status_code=status)

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise CLINetworkError(f"Failed to connect to backend at {self.base_url}. Ensure the server is running on 127.0.0.1.") from exc
        except httpx.ReadTimeout as exc:
            raise CLINetworkError(f"Backend request timed out after {self.timeout.read} seconds.") from exc
        except httpx.RequestError as exc:
            raise CLINetworkError(f"Network error communicating with backend: {str(exc)}") from exc

    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._get_auth_headers(idempotency_key=idempotency_key)
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as client:
                resp = client.request(method, path, json=json_data, params=params, headers=headers)
                return self._handle_response(resp)
        except (CLIError, CLINetworkError):
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise CLINetworkError(f"Failed to connect to backend at {self.base_url}. Ensure server is running.") from exc
        except httpx.ReadTimeout as exc:
            raise CLINetworkError(f"Request timed out.") from exc
        except Exception as exc:
            raise CLINetworkError(f"Unexpected connection error: {str(exc)}") from exc

    # --- System / Doctor ---
    def get_health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/health")

    # --- Authentication ---
    def login(self, email: str, password: str) -> Dict[str, Any]:
        # Login does not require prior Authorization header
        with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as client:
            resp = client.post("/api/auth/login", json={"email": email, "password": password})
            return self._handle_response(resp)

    def logout(self) -> Dict[str, Any]:
        return self._request("POST", "/api/auth/logout")

    def whoami(self) -> Dict[str, Any]:
        return self._request("GET", "/api/auth/me")

    # --- Jobs ---
    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/jobs", params={"limit": limit})

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/jobs/{job_id}")

    def get_active_job(self) -> Optional[Dict[str, Any]]:
        return self._request("GET", "/api/jobs/active")

    def start_job(
        self,
        location: str,
        radius_km: float = 1.5,
        categories: Optional[List[str]] = None,
        max_leads: int = 25,
        filter_only_with_phone: bool = False,
        filter_only_missing_website: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "location": location,
            "radius_km": radius_km,
            "categories": categories or ["cafe", "restaurant", "shop", "salon", "gym", "clinic"],
            "max_leads": max_leads,
            "filter_only_with_phone": filter_only_with_phone,
            "filter_only_missing_website": filter_only_missing_website,
        }
        return self._request("POST", "/api/jobs", json_data=payload, idempotency_key=idempotency_key)

    def pause_job(self, job_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/jobs/{job_id}/pause")

    def resume_job(self, job_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/jobs/{job_id}/resume")

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/jobs/{job_id}/cancel")

    # --- Leads ---
    def list_leads(
        self,
        search: Optional[str] = None,
        category: Optional[str] = None,
        pipeline_status: Optional[str] = None,
        min_score: Optional[int] = None,
        max_score: Optional[int] = None,
        has_phone: Optional[bool] = None,
        missing_website: Optional[bool] = None,
        job_id: Optional[str] = None,
        sort_by: str = "score",
        sort_dir: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "page": page,
            "page_size": page_size,
        }
        if search: params["search"] = search
        if category: params["category"] = category
        if pipeline_status: params["pipeline_status"] = pipeline_status
        if min_score is not None: params["min_score"] = min_score
        if max_score is not None: params["max_score"] = max_score
        if has_phone is not None: params["has_phone"] = has_phone
        if missing_website is not None: params["missing_website"] = missing_website
        if job_id: params["job_id"] = job_id
        return self._request("GET", "/api/leads", params=params)

    def get_lead(self, lead_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/leads/{lead_id}")

    def update_pipeline(
        self,
        lead_id: str,
        pipeline_status: str,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"pipeline_status": pipeline_status}
        if notes is not None:
            payload["notes"] = notes
        return self._request("PATCH", f"/api/leads/{lead_id}/pipeline", json_data=payload, idempotency_key=idempotency_key)

    # --- Auditing ---
    def audit_lead(self, lead_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/leads/{lead_id}/audit")

    def audit_target(self, url: str, business_name: str = "Target Business", category: str = "local_business") -> Dict[str, Any]:
        payload = {"url": url, "business_name": business_name, "category": category}
        return self._request("POST", "/api/leads/audit-target", json_data=payload)

    # --- Export ---
    def export_csv(self, pipeline_status: Optional[str] = None) -> str:
        params = {}
        if pipeline_status:
            params["pipeline_status"] = pipeline_status
        return self._request("GET", "/api/export/csv", params=params)

    # --- Categories ---
    def get_categories(self) -> List[Dict[str, str]]:
        return self._request("GET", "/api/discovery/categories")

    # --- Administration ---
    def get_legacy_summary(self) -> Dict[str, int]:
        return self._request("GET", "/api/auth/admin/legacy-summary")

    def adopt_legacy(self, password: str, confirm: bool = True, bootstrap_token: str = "") -> Dict[str, Any]:
        headers = self._get_auth_headers()
        if bootstrap_token:
            headers["X-Admin-Bootstrap-Token"] = bootstrap_token
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as client:
                resp = client.post(
                    "/api/auth/admin/adopt-legacy",
                    json={"password": password, "confirm": confirm},
                    headers=headers,
                )
                return self._handle_response(resp)
        except (CLIError, CLINetworkError):
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise CLINetworkError(f"Failed to connect to backend at {self.base_url}. Ensure server is running.") from exc
        except httpx.ReadTimeout as exc:
            raise CLINetworkError("Request timed out.") from exc
        except Exception as exc:
            raise CLINetworkError(f"Network error: {str(exc)}") from exc
