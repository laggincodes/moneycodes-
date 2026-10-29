"""
WebsiteLead Hunter - Secure Windows CLI Entrypoint.
Provides unified, authenticated command-line access to discovery, auditing,
jobs, pipeline management, exports, and safe automation.
"""
import argparse
import getpass
import json
import os
import secrets
import sys
import time
from typing import Optional, List

from app.cli.client import (
    LeadHunterClient,
    CLIError,
    CLIAuthError,
    CLIPermissionError,
    CLINotFoundError,
    CLIValidationError,
    CLINetworkError,
)
from app.cli.credentials import (
    save_session_token,
    load_session_token,
    clear_session_token,
    is_credential_store_available,
    DEFAULT_TARGET,
)
from app.cli.formatter import (
    format_lead_table,
    format_lead_detail,
    format_job_table,
    format_job_detail,
    redact_secret,
)
from app.cli.automation import run_automation_plan


def build_parser() -> argparse.ArgumentParser:
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="Output results in JSON format")

    parser = argparse.ArgumentParser(
        prog="hunt",
        description="WebsiteLead Hunter - Secure Windows CLI & Automation Tool",
        parents=[json_parent],
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"),
        help="Backend API base URL (default: http://127.0.0.1:8000 or API_BASE_URL env var)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- auth: login, logout, whoami ---
    p_login = subparsers.add_parser("login", help="Authenticate with the local backend and store session in Windows Credential Manager")
    p_login.add_argument("--email", help="User email address (prompts if omitted)")

    subparsers.add_parser("logout", help="Log out and permanently delete stored session from Windows Credential Manager")
    subparsers.add_parser("whoami", parents=[json_parent], help="Display authenticated user information and session status")

    # --- doctor ---
    subparsers.add_parser("doctor", help="Run local diagnostic checks on backend reachability and credential storage")

    # --- jobs ---
    p_jobs = subparsers.add_parser("jobs", help="Manage background discovery and audit jobs")
    sp_jobs = p_jobs.add_subparsers(dest="jobs_action", required=True)

    sp_jobs_list = sp_jobs.add_parser("list", parents=[json_parent], help="List recent discovery jobs")
    sp_jobs_list.add_argument("--limit", type=int, default=20, help="Max jobs to list (default 20)")

    sp_jobs_show = sp_jobs.add_parser("show", parents=[json_parent], help="Show details and real-time logs for a job")
    sp_jobs_show.add_argument("job_id", help="Discovery Job ID")

    sp_jobs_wait = sp_jobs.add_parser("wait", help="Wait for a job to reach a terminal state")
    sp_jobs_wait.add_argument("job_id", help="Discovery Job ID")
    sp_jobs_wait.add_argument("--timeout", type=float, default=300.0, help="Max seconds to wait (default: 300)")

    sp_jobs_pause = sp_jobs.add_parser("pause", help="Pause a running job")
    sp_jobs_pause.add_argument("job_id", help="Job ID to pause")

    sp_jobs_resume = sp_jobs.add_parser("resume", help="Resume a paused job")
    sp_jobs_resume.add_argument("job_id", help="Job ID to resume")

    sp_jobs_cancel = sp_jobs.add_parser("cancel", help="Cancel a pending or running job")
    sp_jobs_cancel.add_argument("job_id", help="Job ID to cancel")
    sp_jobs_cancel.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # --- leads ---
    p_leads = subparsers.add_parser("leads", help="Inspect and update client acquisition leads")
    sp_leads = p_leads.add_subparsers(dest="leads_action", required=True)

    sp_leads_list = sp_leads.add_parser("list", parents=[json_parent], help="List leads with optional search and filters")
    sp_leads_list.add_argument("--search", help="Search by business name or address")
    sp_leads_list.add_argument("--category", help="Filter by business category")
    sp_leads_list.add_argument("--status", help="Filter by pipeline stage (e.g. new, qualified, contacted, won)")
    sp_leads_list.add_argument("--min-score", type=int, help="Minimum lead score")
    sp_leads_list.add_argument("--max-score", type=int, help="Maximum lead score")
    sp_leads_list.add_argument("--with-phone", action="store_true", help="Only leads with phone number")
    sp_leads_list.add_argument("--missing-website", action="store_true", help="Only leads without a website")
    sp_leads_list.add_argument("--job-id", help="Filter by discovery job ID")
    sp_leads_list.add_argument("--sort-by", default="score", choices=["score", "name", "created_at"], help="Sort column")
    sp_leads_list.add_argument("--sort-dir", default="desc", choices=["asc", "desc"], help="Sort direction")
    sp_leads_list.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    sp_leads_list.add_argument("--page-size", type=int, default=20, help="Page size (default 20)")

    sp_leads_show = sp_leads.add_parser("show", parents=[json_parent], help="Show comprehensive details, audit findings, and pitch for a lead")
    sp_leads_show.add_argument("lead_id", help="Lead ID")

    sp_leads_pipe = sp_leads.add_parser("pipeline", parents=[json_parent], help="Update pipeline stage and notes for a lead")
    sp_leads_pipe.add_argument("lead_id", help="Lead ID")
    sp_leads_pipe.add_argument("status", choices=["new", "qualified", "contacted", "replied", "meeting", "won", "lost"], help="Target pipeline stage")
    sp_leads_pipe.add_argument("--notes", help="Optional sales notes or meeting outcome")
    sp_leads_pipe.add_argument("--idempotency-key", help="Explicit idempotency key (auto-generated if omitted)")
    sp_leads_pipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # --- discover ---
    p_disc = subparsers.add_parser("discover", parents=[json_parent], help="Start or preview a new business discovery and audit job")
    p_disc.add_argument("--location", required=True, help="Location name or address to search (e.g. 'Indiranagar, Bangalore')")
    p_disc.add_argument("--radius", type=float, default=1.5, help="Search radius in kilometers (default: 1.5)")
    p_disc.add_argument("--categories", help="Comma-separated category list (default: cafe,restaurant,shop,salon,gym,clinic)")
    p_disc.add_argument("--max-leads", type=int, default=25, help="Maximum leads to discover (default: 25)")
    p_disc.add_argument("--with-phone", action="store_true", help="Filter: require business phone")
    p_disc.add_argument("--missing-website", action="store_true", help="Filter: only businesses missing a website")
    p_disc.add_argument("--dry-run", action="store_true", help="Preview discovery parameters without creating a job")
    p_disc.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # --- audit ---
    p_audit = subparsers.add_parser("audit", parents=[json_parent], help="Audit website mobile-responsiveness, SSL, and CTAs for a lead or target URL")
    p_audit.add_argument("target", nargs="?", help="Lead ID or target URL")
    p_audit.add_argument("--url", help="Target website URL to audit directly")
    p_audit.add_argument("--name", default="Target Business", help="Business display name")
    p_audit.add_argument("--category", default="local_business", help="Business category")
    p_audit.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt before probing target")

    # --- export ---
    p_exp = subparsers.add_parser("export", help="Export discovered leads to a CSV file")
    p_exp.add_argument("--output", default="leads_export.csv", help="Output file path (default: leads_export.csv)")
    p_exp.add_argument("--status", help="Filter by pipeline stage (optional)")
    p_exp.add_argument("-y", "--yes", action="store_true", help="Overwrite existing output file without confirmation")

    # --- automate ---
    p_auto = subparsers.add_parser("automate", help="Execute automated discovery recipes with safety controls")
    sp_auto = p_auto.add_subparsers(dest="automate_action", required=True)

    sp_auto_plan = sp_auto.add_parser("plan", help="Dry-run preview of an automation recipe")
    sp_auto_plan.add_argument("recipe", help="Path to automation recipe JSON file")

    sp_auto_run = sp_auto.add_parser("run", help="Execute an automation recipe sequentially")
    sp_auto_run.add_argument("recipe", help="Path to automation recipe JSON file")
    sp_auto_run.add_argument("--dry-run", action="store_true", help="Preview tasks without executing")
    sp_auto_run.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # --- admin ---
    p_admin = subparsers.add_parser("admin", help="Administrative operations (host-local only)")
    sp_admin = p_admin.add_subparsers(dest="admin_action", required=True)

    sp_adopt = sp_admin.add_parser(
        "adopt-legacy",
        parents=[json_parent],
        help="Securely adopt unowned legacy leads and jobs into the authenticated account",
    )
    sp_adopt.add_argument("--password", help="Account password for administrative re-verification (prompts securely if omitted)")
    sp_adopt.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation prompt")

    return parser


def get_admin_token_path() -> str:
    """Returns filesystem path for the ephemeral admin bootstrap token."""
    override = os.getenv("ADMIN_TOKEN_PATH")
    if override:
        return os.path.abspath(override)
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_dir, ".admin_token")


def cmd_login(client: LeadHunterClient, args: argparse.Namespace) -> int:
    email = args.email
    if not email:
        try:
            email = input("Enter Email Address: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 4

    if not email:
        print("Error: Email address cannot be empty.", file=sys.stderr)
        return 2

    try:
        password = getpass.getpass("Enter Password: ")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 4

    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        return 2

    try:
        data = client.login(email, password)
        session_token = data.get("session_token")
        if not session_token:
            print("Error: Server did not return a session token.", file=sys.stderr)
            return 1

        # Store in Windows Credential Manager
        try:
            saved = save_session_token(email, session_token)
        except Exception as exc:
            print(f"[X] Credential Storage Error: Failed to save to Windows Credential Manager ({exc}).", file=sys.stderr)
            return 1

        user_info = data.get("user", {})
        if saved:
            print(f"[✓] Login successful. Authenticated as {user_info.get('email')} (ID: {user_info.get('id')}).")
            print("[✓] Session token securely stored in Windows Credential Manager.")
            return 0
        else:
            print("[X] Login failed: Could not persist token to Windows Credential Manager.", file=sys.stderr)
            return 1

    except CLIError as exc:
        print(f"[X] Login failed: {exc.message}", file=sys.stderr)
        return 1


def cmd_logout(client: LeadHunterClient, args: argparse.Namespace) -> int:
    try:
        cred = load_session_token()
    except Exception as exc:
        print(f"[X] Credential Store Error: Failed to read from Windows Credential Manager ({exc})", file=sys.stderr)
        return 1

    if not cred:
        print("No active CLI session found in Windows Credential Manager.")
        return 0

    email, _ = cred
    server_revoked = False
    server_error_msg = None

    try:
        client.logout()
        server_revoked = True
    except CLINetworkError as exc:
        server_error_msg = f"Backend server unreachable ({exc.message}). Server session could not be revoked."
    except CLIError as exc:
        server_error_msg = f"Server returned error during session revocation: {exc.message}"
    except Exception as exc:
        server_error_msg = f"Unexpected error during session revocation: {str(exc)}"

    try:
        cleared = clear_session_token()
    except Exception as exc:
        print(f"[X] Credential Store Error: Failed to delete credential from Windows Credential Manager ({exc})", file=sys.stderr)
        return 1

    if server_revoked and cleared:
        print(f"[✓] Successfully logged out. Confirmed server session revoked and credentials for '{email}' removed from Windows Credential Manager.")
        return 0
    elif not server_revoked and cleared:
        print(f"[!] Warning: {server_error_msg}", file=sys.stderr)
        print(f"[INFO] Local credentials for '{email}' removed from Windows Credential Manager, but server session may remain active until expiration.", file=sys.stderr)
        return 1
    elif server_revoked and not cleared:
        print(f"[!] Warning: Server session revoked, but failed to remove credential entry from Windows Credential Manager.", file=sys.stderr)
        return 1
    else:
        print(f"[X] Logout failed: {server_error_msg} and failed to clear local credentials.", file=sys.stderr)
        return 1


def cmd_whoami(client: LeadHunterClient, args: argparse.Namespace) -> int:
    cred = load_session_token()
    if not cred:
        print("Not logged in. (No credentials stored in Windows Credential Manager)")
        return 3

    stored_email, _token = cred
    try:
        data = client.whoami()
        user = data.get("user", {})
        if args.json:
            print(json.dumps(user, indent=2))
        else:
            print("=" * 60)
            print("AUTHENTICATED CLI SESSION")
            print("=" * 60)
            print(f"User ID:        {user.get('id')}")
            print(f"Email:          {user.get('email')}")
            print(f"Full Name:      {user.get('full_name') or '<none>'}")
            print(f"Account Created:{user.get('created_at')}")
            print(f"Backend Origin: {client.base_url}")
            print(f"Stored Account: {stored_email}")
            print("=" * 60)
        return 0
    except CLIAuthError:
        print("Session has expired or is invalid. Please run 'hunt login' to authenticate.", file=sys.stderr)
        return 3
    except CLIError as exc:
        print(f"Error checking authentication status: {exc.message}", file=sys.stderr)
        return 1


def cmd_doctor(client: LeadHunterClient, args: argparse.Namespace) -> int:
    print("=" * 60)
    print("WEBSITELEAD HUNTER CLI DIAGNOSTIC REPORT")
    print("=" * 60)

    # 1. Check local backend reachability
    backend_ok = False
    try:
        health = client.get_health()
        if health.get("status") == "healthy":
            backend_ok = True
            print(f"[OK] Backend API Reachability: {client.base_url} (status: healthy)")
        else:
            print(f"[WARN] Backend API responded with unexpected status: {health}")
    except Exception as exc:
        print(f"[FAIL] Backend API Reachability: Could not connect to {client.base_url} ({exc})")

    # 2. Check Windows Credential Manager
    cred_avail = is_credential_store_available()
    if cred_avail:
        print(f"[OK] Windows Credential Manager: Available via advapi32.dll (ctypes)")
    else:
        print(f"[FAIL] Windows Credential Manager: Unavailable on this operating system or user context")

    # 3. Check Session / Credential State
    cred = load_session_token()
    if cred:
        email, _ = cred
        print(f"[OK] Stored Credential: Found session for '{email}' in Windows Credential Manager")
        try:
            me = client.whoami()
            user_id = me.get("user", {}).get("id")
            print(f"[OK] Active Session Validation: Valid session for User ID {user_id}")
        except CLIAuthError:
            print(f"[WARN] Active Session Validation: Stored session is expired or revoked. Run 'hunt login'.")
        except Exception as exc:
            print(f"[WARN] Active Session Validation: Could not verify with backend ({exc})")
    else:
        print(f"[INFO] Stored Credential: None. Run 'hunt login' to connect your account.")

    # 4. Check Loopback Safety
    if "127.0.0.1" in client.base_url or "localhost" in client.base_url:
        print(f"[OK] Network Binding Safety: Confirmed bound to local loopback interface ({client.base_url})")
    else:
        print(f"[WARN] Network Binding Safety: Non-loopback URL '{client.base_url}'. Remote transmission requires HTTPS.")

    print("=" * 60)
    return 0 if backend_ok else 1


def cmd_jobs(client: LeadHunterClient, args: argparse.Namespace) -> int:
    action = args.jobs_action
    if action == "list":
        jobs = client.list_jobs(limit=args.limit)
        if args.json:
            print(json.dumps(jobs, indent=2))
        else:
            print(format_job_table(jobs))
        return 0

    elif action == "show":
        job = client.get_job(args.job_id)
        if args.json:
            print(json.dumps(job, indent=2))
        else:
            print(format_job_detail(job))
        return 0

    elif action == "wait":
        print(f"Waiting for job '{args.job_id}' to reach terminal state (timeout: {args.timeout}s)...")
        start_time = time.time()
        while time.time() - start_time < args.timeout:
            job = client.get_job(args.job_id)
            st = job.get("status")
            proc = job.get("processed_count", 0)
            found = job.get("total_found", 0)
            print(f"\rStatus: {st.upper()} | Found: {found} | Processed: {proc}", end="", flush=True)
            if st in ("completed", "failed", "cancelled"):
                print()
                print(f"[✓] Job reached terminal state '{st.upper()}'.")
                return 0
            time.sleep(1.5)
        print("\n[!] Wait timeout exceeded.")
        return 1

    elif action == "pause":
        res = client.pause_job(args.job_id)
        print(f"[✓] Job '{args.job_id}' paused successfully.")
        return 0

    elif action == "resume":
        res = client.resume_job(args.job_id)
        print(f"[✓] Job '{args.job_id}' resumed.")
        return 0

    elif action == "cancel":
        if not args.yes:
            confirm = input(f"Are you sure you want to cancel job '{args.job_id}'? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Action cancelled.")
                return 4
        client.cancel_job(args.job_id)
        print(f"[✓] Job '{args.job_id}' cancelled.")
        return 0

    return 2


def cmd_leads(client: LeadHunterClient, args: argparse.Namespace) -> int:
    action = args.leads_action
    if action == "list":
        data = client.list_leads(
            search=args.search,
            category=args.category,
            pipeline_status=args.status,
            min_score=args.min_score,
            max_score=args.max_score,
            has_phone=True if args.with_phone else None,
            missing_website=True if args.missing_website else None,
            job_id=args.job_id,
            sort_by=args.sort_by,
            sort_dir=args.sort_dir,
            page=args.page,
            page_size=args.page_size,
        )
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            items = data.get("items", [])
            total = data.get("total", 0)
            page = data.get("page", 1)
            total_pages = data.get("total_pages", 1)
            print(format_lead_table(items))
            print(f"\nShowing page {page}/{total_pages} ({len(items)} of {total} total leads)")
        return 0

    elif action == "show":
        lead = client.get_lead(args.lead_id)
        if args.json:
            print(json.dumps(lead, indent=2))
        else:
            print(format_lead_detail(lead))
        return 0

    elif action == "pipeline":
        if not args.yes:
            confirm = input(f"Update lead '{args.lead_id}' pipeline stage to '{args.status}'? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Action cancelled.")
                return 4

        idem_key = args.idempotency_key or f"cli_{int(time.time())}_{args.lead_id[:6]}"
        updated = client.update_pipeline(
            lead_id=args.lead_id,
            pipeline_status=args.status,
            notes=args.notes,
            idempotency_key=idem_key,
        )
        if args.json:
            print(json.dumps(updated, indent=2))
        else:
            print(f"[✓] Lead '{updated.get('name')}' updated to stage '{updated.get('pipeline_status').upper()}'.")
            if updated.get("notes"):
                print(f"    Notes: {updated.get('notes')}")
        return 0

    return 2


def cmd_discover(client: LeadHunterClient, args: argparse.Namespace) -> int:
    cats = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None

    print("=" * 60)
    print("DISCOVERY JOB SPECIFICATION")
    print("=" * 60)
    print(f"Location Query:      {args.location}")
    print(f"Radius:              {args.radius} km")
    print(f"Categories:          {cats or '<default categories>'}")
    print(f"Max Leads:           {args.max_leads}")
    print(f"Require Phone:       {args.with_phone}")
    print(f"Require Missing Web: {args.missing_website}")
    print("=" * 60)

    if args.dry_run:
        print("[DRY-RUN] Plan inspected. No background job was created.")
        return 0

    if not args.yes:
        confirm = input("Confirm starting discovery and website audit job on 127.0.0.1? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Action cancelled.")
            return 4

    job = client.start_job(
        location=args.location,
        radius_km=args.radius,
        categories=cats,
        max_leads=args.max_leads,
        filter_only_with_phone=args.with_phone,
        filter_only_missing_website=args.missing_website,
    )
    job_id = job.get("id")
    print(f"[✓] Discovery job initiated successfully.")
    print(f"    Job ID: {job_id}")
    print(f"    Monitor progress with: hunt jobs wait {job_id}")
    return 0


def cmd_audit(client: LeadHunterClient, args: argparse.Namespace) -> int:
    target = args.url or args.target
    if not target:
        print("Error: Target lead ID or --url must be specified.", file=sys.stderr)
        return 2

    # Check if target is a URL or lead ID
    is_url = target.startswith("http://") or target.startswith("https://")

    if not args.yes:
        target_desc = f"URL '{target}'" if is_url else f"Lead '{target}'"
        confirm = input(f"Perform website audit on {target_desc}? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Action cancelled.")
            return 4

    if is_url:
        print(f"Auditing website '{target}' via SSRF-hardened pipeline...")
        result = client.audit_target(url=target, business_name=args.name, category=args.category)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("=" * 60)
            print("AUDIT RESULT")
            print("=" * 60)
            print(f"Target URL:         {target}")
            print(f"Reachable:          {result.get('is_reachable')}")
            print(f"Status Code:        {result.get('status_code')}")
            print(f"SSL Valid:          {result.get('ssl_valid')}")
            print(f"Mobile Viewport:    {result.get('has_viewport_meta')}")
            print(f"Clear CTA:          {result.get('has_clear_cta')}")
            print(f"Page Title:         {result.get('title') or '<none>'}")
            findings = result.get("findings") or []
            if findings:
                print("\nFindings:")
                for f in findings:
                    print(f"  [{f.get('severity', '').upper()}] {f.get('message')}")
            print("=" * 60)
        return 0
    else:
        print(f"Re-auditing Lead '{target}'...")
        lead = client.audit_lead(lead_id=target)
        if args.json:
            print(json.dumps(lead, indent=2))
        else:
            print(f"[✓] Lead '{lead.get('name')}' re-audited. New score: {lead.get('score')}/100.")
            print(format_lead_detail(lead))
        return 0


def cmd_export(client: LeadHunterClient, args: argparse.Namespace) -> int:
    output_path = os.path.abspath(args.output)
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        print(f"Error: Output directory '{output_dir}' does not exist.", file=sys.stderr)
        return 2

    if os.path.exists(output_path) and not args.yes:
        confirm = input(f"File '{output_path}' already exists. Overwrite? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Export cancelled.")
            return 4

    csv_data = client.export_csv(pipeline_status=args.status)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        f.write(csv_data)

    byte_count = os.path.getsize(output_path)
    print(f"[✓] Successfully exported {byte_count} bytes to: {output_path}")
    return 0


def cmd_automate(client: LeadHunterClient, args: argparse.Namespace) -> int:
    action = args.automate_action
    if action == "plan":
        return run_automation_plan(client, args.recipe, dry_run=True, yes=True)
    elif action == "run":
        return run_automation_plan(client, args.recipe, dry_run=args.dry_run, yes=args.yes)
    return 2


def cmd_admin(client: LeadHunterClient, args: argparse.Namespace) -> int:
    if args.admin_action == "adopt-legacy":
        return cmd_admin_adopt_legacy(client, args)
    return 2


def cmd_admin_adopt_legacy(client: LeadHunterClient, args: argparse.Namespace) -> int:
    cred = load_session_token()
    if not cred:
        print("[X] Authentication Error: You must be logged in as an administrator to run this command. Please run 'hunt login' first.", file=sys.stderr)
        return 3

    # 1. Fetch unowned count summary
    try:
        summary = client.get_legacy_summary()
    except CLIAuthError as exc:
        print(f"[X] Authentication Error: {exc.message}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"[X] Error checking legacy inventory: {exc}", file=sys.stderr)
        return 1

    unowned_leads = summary.get("unowned_leads", 0)
    unowned_jobs = summary.get("unowned_jobs", 0)

    if unowned_leads == 0 and unowned_jobs == 0:
        if getattr(args, "json", False):
            print(json.dumps({"message": "No unowned legacy records found.", "adopted_leads": 0, "adopted_jobs": 0}))
        else:
            print("[i] No unowned legacy leads or jobs found in the database. Nothing to adopt.")
        return 0

    if not getattr(args, "json", False):
        print(f"[i] Discovered unowned legacy data: {unowned_leads} lead(s) and {unowned_jobs} job(s).")

    # 2. Interactive confirmation unless -y is specified
    if not args.yes:
        try:
            ans = input(f"Are you sure you want to adopt these {unowned_leads} lead(s) and {unowned_jobs} job(s) into your account? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 4
        if ans not in ("y", "yes"):
            print("[i] Adoption cancelled by user. No changes made.")
            return 0

    # 3. Secure password re-verification prompt
    password = args.password
    if not password:
        try:
            password = getpass.getpass("Enter your account password for administrative re-verification: ")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 4

    if not password:
        print("[X] Error: Password cannot be empty for administrative re-verification.", file=sys.stderr)
        return 2

    # 4. Generate ephemeral unforgeable token on local host disk
    token = secrets.token_hex(32)
    token_path = get_admin_token_path()
    try:
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception as exc:
        print(f"[X] Host Security Error: Could not write local bootstrap token ({exc}).", file=sys.stderr)
        return 1

    # 5. Submit adoption mutation
    try:
        result = client.adopt_legacy(password=password, confirm=True, bootstrap_token=token)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"[✓] {result.get('message', 'Legacy data adoption successful.')}")
        return 0
    finally:
        # Ensure disk token is cleaned up
        if os.path.exists(token_path):
            try:
                os.remove(token_path)
            except OSError:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    client = LeadHunterClient(base_url=args.base_url)

    try:
        if args.command == "login":
            return cmd_login(client, args)
        elif args.command == "logout":
            return cmd_logout(client, args)
        elif args.command == "whoami":
            return cmd_whoami(client, args)
        elif args.command == "doctor":
            return cmd_doctor(client, args)
        elif args.command == "jobs":
            return cmd_jobs(client, args)
        elif args.command == "leads":
            return cmd_leads(client, args)
        elif args.command == "discover":
            return cmd_discover(client, args)
        elif args.command == "audit":
            return cmd_audit(client, args)
        elif args.command == "export":
            return cmd_export(client, args)
        elif args.command == "automate":
            return cmd_automate(client, args)
        elif args.command == "admin":
            return cmd_admin(client, args)
        else:
            parser.print_help()
            return 2

    except CLIAuthError as exc:
        print(f"[X] Authentication Error: {exc.message}", file=sys.stderr)
        return 3
    except CLIPermissionError as exc:
        print(f"[X] Permission Denied: {exc.message}", file=sys.stderr)
        return 1
    except CLINotFoundError as exc:
        print(f"[X] Not Found: {exc.message}", file=sys.stderr)
        return 1
    except CLIValidationError as exc:
        print(f"[X] Validation Error: {exc.message}", file=sys.stderr)
        return 2
    except CLINetworkError as exc:
        print(f"[X] Network Connection Error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        return 4
    except Exception as exc:
        print(f"[X] Unexpected Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
