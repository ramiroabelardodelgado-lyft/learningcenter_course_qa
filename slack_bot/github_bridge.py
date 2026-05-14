#!/usr/bin/env python3
"""
github_bridge.py
================
Standalone bridge between GitHub Issues and the S3 job queue.

This script is a TRANSLATOR — it doesn't run the pipeline itself.
It converts GitHub Issues into S3 pending jobs (which poller.py picks up),
and converts S3 complete results back into GitHub Issue comments + CSV uploads.

Removable: If Workato gets S3 access later, just stop running this script.
Nothing else in the pipeline changes.

Flow:
    GitHub Issue (pending)
         │
         ▼
    github_bridge.py ──writes──▶ S3 course-qa/pending/{job_id}.json
         │                              │
         │                              ▼
         │                        poller.py (existing, unchanged)
         │                              │
         │                              ▼
         │                        runner.py (existing, unchanged)
         │                              │
         │                              ▼
         ◀──reads───────────── S3 course-qa/complete/{job_id}.json
         │
         ▼
    GitHub Issue: comment summary + upload CSV + close issue
         │
         ▼
    Slack webhook: summary + CSV links → #learning-center-qa

Usage (on instance):
    source ~/.bashrc
    cd $HOME/studio
    nohup python slack_bot/github_bridge.py > $HOME/studio/github_bridge.log 2>&1 &

Environment variables (add to ~/studio/.env):
    GITHUB_TOKEN=ghp_your_token
    GITHUB_API=https://api.github.com
    GITHUB_REPO=ramiroabelardodelgado-lyft/lyftlearn-qa-jobs
    WORKATO_CALLBACK_URL=https://hooks.slack.com/triggers/...  (Slack Workflow webhook)

    # Existing (already set):
    # AWS credentials come from container role — no config needed
"""

import os
import sys
import json
import re
import time
import base64
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# Try loading .env
try:
    env_path = Path.home() / "studio" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip()
except Exception:
    pass

import boto3


# ═══════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

S3_BUCKET = os.environ.get("S3_BUCKET", "lyft-lyftlearn-production-iad")
S3_PENDING_PREFIX = os.environ.get("S3_PENDING_PREFIX", "course-qa/pending/")
S3_COMPLETE_PREFIX = os.environ.get("S3_COMPLETE_PREFIX", "course-qa/complete/")
S3_CSV_PREFIX = os.environ.get("S3_CSV_PREFIX", "course-qa/csvs/")

WORKATO_CALLBACK_URL = os.environ.get("WORKATO_CALLBACK_URL", "")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

# Track which jobs we've already bridged (in memory — resets on restart)
# Maps job_id → github_issue_number
active_jobs = {}

# Track original job params (for callback with Slack IDs)
# Maps job_id → job params dict
job_params = {}


# ═══════════════════════════════════════════════════════════════════════
# GitHub API helpers
# ═══════════════════════════════════════════════════════════════════════

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def gh_get(path, params=None):
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.get(url, headers=gh_headers(), params=params, timeout=15)
    return resp


def gh_post(path, data):
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.post(url, headers=gh_headers(), json=data, timeout=15)
    return resp


def gh_patch(path, data):
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.patch(url, headers=gh_headers(), json=data, timeout=15)
    return resp


def gh_delete(path):
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.delete(url, headers=gh_headers(), timeout=15)
    return resp


def gh_put(path, data):
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.put(url, headers=gh_headers(), json=data, timeout=15)
    return resp


def repo_path(suffix):
    """Build /repos/{owner}/{repo}/{suffix}"""
    if suffix:
        return f"/repos/{GITHUB_REPO}/{suffix}"
    return f"/repos/{GITHUB_REPO}"


# ═══════════════════════════════════════════════════════════════════════
# S3 helpers
# ═══════════════════════════════════════════════════════════════════════

def get_s3():
    return boto3.client("s3")


def s3_write_json(s3, key, data):
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType="application/json",
    )


def s3_read_json(s3, key):
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def s3_list_keys(s3, prefix):
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def s3_delete(s3, key):
    s3.delete_object(Bucket=S3_BUCKET, Key=key)


def s3_read_bytes(s3, key):
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return resp["Body"].read()


# ═══════════════════════════════════════════════════════════════════════
# INBOUND: GitHub Issues → S3 pending
# ═══════════════════════════════════════════════════════════════════════

def parse_job_from_body(body):
    """Extract JSON from GitHub Issue body (inside ```json fences or raw)."""
    if not body:
        return None
    # Try code-fenced JSON first
    match = re.search(r'```json?\s*\n(.*?)\n```', body, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: try entire body as JSON
    try:
        return json.loads(body.strip())
    except json.JSONDecodeError:
        return None


def poll_github_pending():
    """
    Find GitHub Issues with 'pending' label.
    For each: parse job params, write to S3 pending, update labels.
    """
    resp = gh_get(repo_path("issues"), params={
        # "labels": "pending",
        "state": "open",
        "per_page": 5,
    })

    if resp.status_code != 200:
        log(f"GitHub API error: {resp.status_code} — {resp.text[:200]}")
        return

    issues = resp.json()
    if not issues:
        return

    s3 = get_s3()

    for issue in issues:
        issue_number = issue["number"]
        title = issue.get("title", "")
        body = issue.get("body") or ""

        log(f"Found pending issue #{issue_number}: {title}")

        # Skip issues already being processed (prevents double-run)
        if issue_number in active_jobs.values():
            log(f"  ⏭️  Issue #{issue_number} already in progress — skipping")
            continue

        # Parse job params
        job = parse_job_from_body(body)
        if not job:
            log(f"  ❌ Could not parse job JSON from issue #{issue_number}")
            fail_issue(issue_number, "Could not parse job JSON from issue body. Expected JSON in the issue body.")
            continue

        # Infer job_type from title if not in JSON (for backwards compatibility)
        if not job.get("job_type"):
            if title.lower().startswith("screenshot"):
                job["job_type"] = "screenshots"
            else:
                job["job_type"] = "qa"

        # Ensure job_id exists — use timestamp for uniqueness
        if not job.get("job_id"):
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            job["job_id"] = f"gh-{issue_number}-{timestamp}"

        job_id = job["job_id"]

        # Store the issue number in the job so we can find it later
        job["github_issue_number"] = issue_number

        # Track locally
        active_jobs[job_id] = issue_number
        job_params[job_id] = job.copy()

        # Write to S3 pending (poller.py will pick this up)
        s3_key = f"{S3_PENDING_PREFIX}{job_id}.json"
        try:
            s3_write_json(s3, s3_key, job)
            log(f"  ✅ Wrote to S3: {s3_key}")
        except Exception as e:
            log(f"  ❌ S3 write failed: {e}")
            fail_issue(issue_number, f"Failed to write job to S3: {e}")
            continue

        # Update GitHub issue: pending → running
        gh_delete(repo_path(f"issues/{issue_number}/labels/pending"))
        gh_post(repo_path(f"issues/{issue_number}/labels"), {"labels": ["running"]})

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        gh_post(repo_path(f"issues/{issue_number}/comments"), {
            "body": f"🔄 Job `{job_id}` submitted to pipeline at {now}\n\nWaiting for instance to pick it up..."
        })

        log(f"  🏷️  Issue #{issue_number} → running")


# ═══════════════════════════════════════════════════════════════════════
# OUTBOUND: S3 complete → GitHub Issue close + CSV upload + Slack
# ═══════════════════════════════════════════════════════════════════════

def poll_s3_complete():
    """
    Check S3 course-qa/complete/ for finished jobs.
    For each: read result, update GitHub issue, upload CSV, close issue,
    and POST to Slack webhook.
    """
    s3 = get_s3()
    keys = s3_list_keys(s3, S3_COMPLETE_PREFIX)

    for key in keys:
        if not key.endswith(".json"):
            continue

        try:
            result = s3_read_json(s3, key)
        except Exception as e:
            log(f"  ⚠️  Could not read {key}: {e}")
            continue

        job_id = result.get("job_id", "")
        status = result.get("status", "unknown")
        issue_number = result.get("github_issue_number")

        # Also check our in-memory map
        if not issue_number and job_id in active_jobs:
            issue_number = active_jobs[job_id]

        if not issue_number:
            # Not a GitHub-originated job — skip (let poller handle it)
            continue

        log(f"Found completed job: {job_id} (issue #{issue_number}, status: {status})")

        csv_links = []

        try:
            job_type = result.get("job_type", "qa")

            if status == "error":
                error_msg = result.get("error", "Unknown error")
                fail_issue(issue_number, f"Pipeline error:\n```\n{error_msg}\n```")
                post_slack_callback(job_id, "error", f"❌ Pipeline failed: {error_msg}", [], result)

            elif job_type == "screenshots":
                # ── Screenshot job: presigned ZIP link instead of CSV ──────
                summary = build_screenshot_summary(result)
                zip_key = result.get("s3_zip_key")
                download_link = ""
                zip_links = []

                if zip_key:
                    try:
                        presigned = s3.generate_presigned_url(
                            "get_object",
                            Params={"Bucket": S3_BUCKET, "Key": zip_key},
                            ExpiresIn=3600,   # 1 hour
                        )
                        download_link = (
                            f"\n\n📦 **[Download Screenshots ZIP]({presigned})**"
                            f" *(link expires in 1hr)*"
                        )
                        # Pass presigned URL to Slack callback as if it were a CSV
                        zip_links = [("screenshots.zip", presigned)]
                    except Exception as e:
                        log(f"  ⚠️  Could not generate presigned URL: {e}")

                comment_body = summary + download_link
                complete_issue(issue_number, comment_body)
                post_slack_callback(job_id, "complete", summary, zip_links, result)

            else:
                # ── QA job: existing behavior, unchanged ───────────────────
                # Build summary comment (summary field may be a dict, not a string)
                summary = result.get("summary", "")
                if not isinstance(summary, str) or not summary:
                    summary = build_summary_from_result(result)

                # Upload CSVs to GitHub repo (with timestamp in filename for versioning)
                csv_links = upload_csvs_to_github(s3, job_id)

                # Build full comment
                comment_body = summary
                if csv_links:
                    comment_body += "\n\n### 📎 Reports\n"
                    for name, url in csv_links:
                        comment_body += f"- [{name}]({url})\n"

                # Post comment + close issue
                complete_issue(issue_number, comment_body)

                # POST to Slack webhook
                post_slack_callback(job_id, "complete", summary, csv_links, result)

            # Clean up: remove from S3 complete/ and active_jobs
            s3_delete(s3, key)
            active_jobs.pop(job_id, None)
            job_params.pop(job_id, None)
            log(f"  ✅ Issue #{issue_number} closed, S3 result cleaned up")

        except Exception as e:
            log(f"  ❌ Error processing result for issue #{issue_number}: {e}")
            traceback.print_exc()

def build_summary_from_result(result):
    """Build a markdown summary table from the result JSON."""
    # The S3 complete JSON has summary as a dict of locale → counts
    # e.g. {"summary": {"es": {"WRONG_LANGUAGE": 0, ...}, "pt": {...}}}
    summary_data = result.get("summary", {})
    course_name = result.get("course_name", "?")

    # If summary is a string, return it directly
    if isinstance(summary_data, str) and summary_data:
        return summary_data

    # If summary is not a dict of locales, return simple message
    if not isinstance(summary_data, dict) or not summary_data:
        return f"✅ QA complete for job `{result.get('job_id', '?')}` — {course_name}"

    lines = []
    total_critical = 0

    lines.append("| Locale | ❌ Wrong | ⚠️ Escape | 🔄 Untrans | Status |")
    lines.append("|--------|----------|-----------|------------|--------|")

    for locale, counts in summary_data.items():
        if not isinstance(counts, dict):
            continue
        wrong = counts.get("WRONG_LANGUAGE", 0)
        escape = counts.get("ESCAPE_CHARS", 0)
        untrans = counts.get("UNTRANSLATED", 0)
        critical = wrong + untrans
        total_critical += critical
        status = "✅" if critical == 0 else "❌"
        if critical == 0 and escape > 0:
            status = "⚠️"
        lines.append(f"| {locale} | {wrong} | {escape} | {untrans} | {status} |")

    emoji = "✅" if total_critical == 0 else "❌"
    header = f"{emoji} *QA Complete: {course_name}*\n\n"
    table = "\n".join(lines)
    footer = f"\n\n*Critical issues:* {total_critical}"

    return header + table + footer

def build_screenshot_summary(result):
    """Build Slack summary for a completed screenshots job."""
    job_id   = result.get("job_id", "?")
    course_id = result.get("course_id", "?")
    per_locale = result.get("per_locale_counts", {})
    total    = result.get("total_pages", 0)
    duration = result.get("duration_seconds", 0)

    lines = [f"## 📸 Screenshots Complete: `{course_id}`\n"]
    lines.append("| Locale | Pages |")
    lines.append("|--------|-------|")
    for locale, count in per_locale.items():
        status = "✅" if count > 0 else "❌"
        lines.append(f"| {locale} | {status} {count} |")

    lines.append(f"\n**Total:** {total} screenshots · {duration}s")
    return "\n".join(lines)

def upload_csvs_to_github(s3, job_id):
    """
    Upload CSV files from S3 to the GitHub repo under results/{job_id}/.
    Uses timestamp in filename so every run creates unique files for versioning.
    Returns list of (filename, github_url) tuples.
    """
    csv_links = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Look for CSVs in S3
    csv_keys = s3_list_keys(s3, f"{S3_CSV_PREFIX}{job_id}")
    if not csv_keys:
        # Also try broader search
        csv_keys = s3_list_keys(s3, S3_CSV_PREFIX)
        csv_keys = [k for k in csv_keys if job_id in k]

    if not csv_keys:
        log(f"  ⚠️  No CSVs found in S3 for job {job_id}")
        return csv_links

    for csv_key in csv_keys:
        if not csv_key.endswith(".csv"):
            continue

        try:
            csv_bytes = s3_read_bytes(s3, csv_key)
            original_filename = csv_key.split("/")[-1]

            # Add timestamp to filename for versioning:
            # e.g. "qa_full.csv" → "qa_full_20260302_164700.csv"
            name_part, ext = os.path.splitext(original_filename)
            versioned_filename = f"{name_part}_{timestamp}{ext}"

            # Store under results/{job_id}/
            repo_file_path = f"results/{job_id}/{versioned_filename}"

            # Upload via GitHub Contents API
            content_b64 = base64.b64encode(csv_bytes).decode("utf-8")
            resp = gh_put(repo_path(f"contents/{repo_file_path}"), {
                "message": f"QA results: {job_id}/{versioned_filename}",
                "content": content_b64,
            })

            if resp.status_code in (200, 201):
                github_url = f"https://github.com/{GITHUB_REPO}/blob/main/{repo_file_path}"
                csv_links.append((versioned_filename, github_url))
                log(f"  📎 Uploaded {versioned_filename} → {github_url}")
            else:
                log(f"  ⚠️  CSV upload failed for {versioned_filename}: {resp.status_code} — {resp.text[:200]}")

        except Exception as e:
            log(f"  ⚠️  CSV upload error for {csv_key}: {e}")

    return csv_links


# ═══════════════════════════════════════════════════════════════════════
# Slack Webhook Callback
# ═══════════════════════════════════════════════════════════════════════

def post_slack_callback(job_id, status, summary, csv_links, result=None):
    """
    POST results to Slack Workflow webhook.
    Sends flat key-value pairs matching the Slack Workflow variables:
      summary, course_name, csv_url, github_issue, status
    """
    if not WORKATO_CALLBACK_URL:
        log(f"  ℹ️  No WORKATO_CALLBACK_URL — skipping Slack callback")
        return

    # Get original job params (for GitHub-originated jobs)
    original = job_params.get(job_id, {})
    issue_number = active_jobs.get(job_id, "")

    # If we have a result dict (from S3), prefer it over job_params
    if result:
        course_name = result.get("course_name") or result.get("course_id", "unknown")
    else:
        course_name = original.get("course_name", "") or original.get("course_id", "unknown")

    # Pick the issues CSV URL (most useful for the team)
    # Fall back to full CSV, then empty string
    csv_url = ""
    for name, url in csv_links:
        if "issues" in name.lower():
            csv_url = url
            break
    if not csv_url and csv_links:
        csv_url = csv_links[0][1]

    # Flat payload — matches Slack Workflow webhook variables exactly
    payload = {
        "summary": summary,
        "course_name": course_name,
        "csv_url": csv_url,
        "github_issue": f"https://github.com/{GITHUB_REPO}/issues/{issue_number}" if issue_number else "",
        "status": status,
    }

    try:
        resp = requests.post(WORKATO_CALLBACK_URL, json=payload, timeout=15)
        if resp.status_code in (200, 201, 202):
            log(f"  📨 Slack callback sent (status: {resp.status_code})")
        else:
            log(f"  ⚠️  Slack callback failed: {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log(f"  ⚠️  Slack callback error: {e}")


# ═══════════════════════════════════════════════════════════════════════
# GitHub Issue state changes
# ═══════════════════════════════════════════════════════════════════════

def complete_issue(issue_number, comment_body):
    """Post results comment, add 'complete' label, close issue."""
    gh_post(repo_path(f"issues/{issue_number}/comments"), {"body": comment_body})
    gh_delete(repo_path(f"issues/{issue_number}/labels/running"))
    gh_post(repo_path(f"issues/{issue_number}/labels"), {"labels": ["complete"]})
    gh_patch(repo_path(f"issues/{issue_number}"), {"state": "closed"})


def fail_issue(issue_number, error_message):
    """Post error comment, add 'failed' label, close issue."""
    gh_post(repo_path(f"issues/{issue_number}/comments"), {
        "body": f"❌ {error_message}"
    })
    # Remove pending/running if present (ignore errors)
    gh_delete(repo_path(f"issues/{issue_number}/labels/pending"))
    gh_delete(repo_path(f"issues/{issue_number}/labels/running"))
    gh_post(repo_path(f"issues/{issue_number}/labels"), {"labels": ["failed"]})
    gh_patch(repo_path(f"issues/{issue_number}"), {"state": "closed"})


# ═══════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[github_bridge {ts}] {msg}", flush=True)


def validate_config():
    """Check required config before starting."""
    errors = []
    if not GITHUB_TOKEN:
        errors.append("GITHUB_TOKEN not set")
    if not GITHUB_REPO:
        errors.append("GITHUB_REPO not set")

    # Test GitHub connectivity
    if GITHUB_TOKEN and GITHUB_REPO:
        resp = gh_get(repo_path(""))
        if resp.status_code == 200:
            log(f"✅ GitHub: connected to {GITHUB_REPO}")
        elif resp.status_code == 401:
            errors.append(f"GitHub auth failed (401) — check GITHUB_TOKEN")
        elif resp.status_code == 404:
            errors.append(f"GitHub repo not found (404) — check GITHUB_REPO: {GITHUB_REPO}")
        else:
            errors.append(f"GitHub API returned {resp.status_code}")

    # Test S3 connectivity
    try:
        s3 = get_s3()
        s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=S3_PENDING_PREFIX, MaxKeys=1)
        log(f"✅ S3: connected to {S3_BUCKET}")
    except Exception as e:
        errors.append(f"S3 connection failed: {e}")

    # Slack callback (optional)
    if WORKATO_CALLBACK_URL:
        log(f"✅ Slack callback: {WORKATO_CALLBACK_URL[:60]}...")
    else:
        log(f"ℹ️  No WORKATO_CALLBACK_URL — results will only go to GitHub")

    if errors:
        for err in errors:
            log(f"❌ {err}")
        return False
    return True


def main():
    log("Starting GitHub ↔ S3 bridge")
    log(f"  GitHub repo: {GITHUB_REPO}")
    log(f"  S3 bucket:   {S3_BUCKET}")
    log(f"  Poll every:  {POLL_INTERVAL}s")
    log("")

    if not validate_config():
        log("Fix configuration errors above and restart.")
        sys.exit(1)

    log(f"Polling started — watching for pending issues...\n")

    while True:
        try:
            # INBOUND: GitHub pending issues → S3 pending jobs
            poll_github_pending()

            # OUTBOUND: S3 complete results → GitHub issue close + CSV upload
            poll_s3_complete()

        except KeyboardInterrupt:
            log("Shutting down.")
            break
        except Exception as e:
            log(f"⚠️  Unexpected error in poll loop: {e}")
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()