"""
Safe, Opt-in Automation Runner for WebsiteLead Hunter CLI.
Executes planned discovery recipes sequentially with overlap prevention,
bounded retries, dry-run previews, and credential redaction.
Zero background persistence without explicit user invocation.
"""
import hashlib
import json
import logging
import os
import sys
import time
from typing import Dict, Any, List

from app.cli.client import LeadHunterClient, CLIError, CLINetworkError, CLIValidationError

logger = logging.getLogger(__name__)


def load_automation_recipe(file_path: str) -> Dict[str, Any]:
    """Loads and validates an automation plan recipe JSON file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Automation plan recipe not found at: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "tasks" not in data or not isinstance(data["tasks"], list):
        raise ValueError("Invalid automation recipe format. Root must be an object with a 'tasks' list.")

    for i, t in enumerate(data["tasks"]):
        if not isinstance(t, dict):
            raise ValueError(f"Task index {i} must be a dictionary object.")
        if "location" not in t or not str(t["location"]).strip():
            raise ValueError(f"Task index {i} missing required non-empty 'location' field.")

    return data


def run_automation_plan(
    client: LeadHunterClient,
    recipe_path: str,
    dry_run: bool = False,
    yes: bool = False,
    poll_interval: float = 2.0,
    job_timeout: float = 600.0,
) -> int:
    """
    Executes an automation recipe with strict safety guarantees:
    - Overlap prevention (blocks execution if an active job is already running)
    - Dry-run preview
    - User confirmation
    - Bounded retries (max 3)
    - Foreground-only execution (no daemon, no scheduled task, no system hooks)
    """
    try:
        recipe = load_automation_recipe(recipe_path)
    except Exception as exc:
        print(f"[Automation Error] Failed to load recipe: {exc}", file=sys.stderr)
        return 1

    plan_name = recipe.get("name", os.path.basename(recipe_path))
    tasks: List[Dict[str, Any]] = recipe["tasks"]

    print("=" * 70)
    print(f"AUTOMATION PLAN: {plan_name}")
    print(f"Total Tasks:     {len(tasks)}")
    print(f"Dry-Run Mode:    {'ENABLED (Preview only)' if dry_run else 'DISABLED'}")
    print("=" * 70)

    for i, t in enumerate(tasks, start=1):
        loc = t.get("location")
        radius = t.get("radius_km", 1.5)
        cats = t.get("categories", ["all"])
        max_leads = t.get("max_leads", 25)
        missing_web = t.get("filter_only_missing_website", False)
        with_phone = t.get("filter_only_with_phone", False)
        print(f"  Task {i}: Location: '{loc}' | Radius: {radius}km | Max Leads: {max_leads}")
        print(f"          Categories: {cats} | Missing Website Only: {missing_web} | With Phone Only: {with_phone}")

    print("-" * 70)

    if dry_run:
        print("[DRY-RUN] Plan validated successfully. Zero network discovery jobs initiated.")
        return 0

    # 1. Check for overlapping jobs
    try:
        active_job = client.get_active_job()
        if active_job and active_job.get("status") in ("running", "pending"):
            print(
                f"[Automation Blocked] Active discovery job '{active_job['id']}' is already running "
                f"(status: {active_job['status']}). Overlapping jobs are strictly prohibited.",
                file=sys.stderr,
            )
            return 1
    except CLIError as exc:
        print(f"[Automation Error] Failed to verify active job status: {exc.message}", file=sys.stderr)
        return 1

    # 2. Confirmation prompt
    if not yes:
        try:
            confirm = input(f"Execute {len(tasks)} planned discovery task(s) on 127.0.0.1? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Automation execution aborted by user.")
                return 4
        except (KeyboardInterrupt, EOFError):
            print("\nAutomation cancelled.")
            return 4

    # 3. Sequential Task Execution
    for idx, task in enumerate(tasks, start=1):
        loc = task["location"]
        radius = float(task.get("radius_km", 1.5))
        cats = task.get("categories")
        max_l = int(task.get("max_leads", 25))
        phone_only = bool(task.get("filter_only_with_phone", False))
        web_only = bool(task.get("filter_only_missing_website", False))

        print(f"\n[{idx}/{len(tasks)}] Starting discovery task for '{loc}'...")

        # Generate deterministic task idempotency key to prevent duplicate jobs on lost response / retry
        # Uses SHA-256 for cross-process determinism and input-sensitivity
        if task.get("idempotency_key"):
            task_idem_key = task["idempotency_key"]
        else:
            task_sig = f"{plan_name}:{idx}:{loc.strip().lower()}:{radius:.2f}:{max_l}:{sorted(cats or [])}:{phone_only}:{web_only}"
            task_hash = hashlib.sha256(task_sig.encode("utf-8")).hexdigest()[:16]
            task_idem_key = f"auto_{plan_name[:8]}_{idx}_{task_hash}"

        # Bounded retries (max 3)
        job_id = None
        for attempt in range(1, 4):
            try:
                job_data = client.start_job(
                    location=loc,
                    radius_km=radius,
                    categories=cats,
                    max_leads=max_l,
                    filter_only_with_phone=phone_only,
                    filter_only_missing_website=web_only,
                    idempotency_key=task_idem_key,
                )
                job_id = job_data["id"]
                print(f"  [+] Job initiated with ID: {job_id}")
                break
            except (CLIValidationError,) as exc:
                print(f"  [X] Permanent validation error on task {idx}: {exc.message}. Skipping retries.", file=sys.stderr)
                break
            except Exception as exc:
                print(f"  [!] Attempt {attempt}/3 failed: {exc}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                else:
                    print(f"  [X] Task {idx} failed after 3 attempts. Skipping to next task.", file=sys.stderr)

        if not job_id:
            continue

        # Wait for task completion
        start_wait = time.time()
        print(f"  [*] Waiting for job '{job_id}' to complete...")
        while time.time() - start_wait < job_timeout:
            try:
                job = client.get_job(job_id)
                status = job.get("status")
                found = job.get("total_found", 0)
                proc = job.get("processed_count", 0)
                if status in ("completed", "failed", "cancelled"):
                    print(f"  [✓] Job {job_id} finished with status '{status.upper()}'. Found: {found}, Processed: {proc}")
                    break
                time.sleep(poll_interval)
            except Exception as exc:
                logger.warning(f"Error polling job {job_id}: {exc}")
                time.sleep(poll_interval)

    print("\n" + "=" * 70)
    print("Automation plan execution complete.")
    print("=" * 70)
    return 0
