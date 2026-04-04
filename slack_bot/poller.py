#!/usr/bin/env python3
"""
S3 job poller — runs continuously on the instance.
Every 30s checks s3://lyft-lyftlearn-production-iad/course-qa/pending/
Picks up job files, runs runner.py, writes result to complete/
"""
import os
import sys
import json
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone

# sys.path patch — packages live inside studio/ (Roadblock #9)
_home   = Path.home()
_studio = _home / "studio"
for _p in [_studio / "persistent-packages", _studio]:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import boto3

BUCKET   = "lyft-lyftlearn-production-iad"
PENDING  = "course-qa/pending/"
COMPLETE = "course-qa/complete/"
POLL_INTERVAL = 30  # seconds


def poll_once(s3):
    """Check for pending jobs and process any found."""
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PENDING)
    items = resp.get("Contents", [])

    if not items:
        return

    for obj in items:
        key = obj["Key"]
        if not key.endswith(".json"):
            continue

        job_id = key.split("/")[-1].replace(".json", "")
        print(f"\n[poller] 📥 Found job: {job_id}", flush=True)

        # Download job params
        try:
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            params = json.loads(body)
        except Exception as e:
            print(f"[poller] ❌ Failed to read job {job_id}: {e}", flush=True)
            continue

        # Delete pending file immediately so no other process picks it up
        s3.delete_object(Bucket=BUCKET, Key=key)
        print(f"[poller] 🗑️  Deleted pending/{job_id}.json", flush=True)

        # Run the pipeline
        result = run_job(params)

        # Write result to complete/
        result_key = f"{COMPLETE}{job_id}.json"
        s3.put_object(
            Bucket=BUCKET,
            Key=result_key,
            Body=json.dumps(result).encode(),
            ContentType="application/json",
        )
        print(f"[poller] ✅ Result written to {result_key}", flush=True)

        # Also upload the CSV to S3 so Workato can attach it to Slack
        if result.get("status") == "success":
            _upload_csvs(s3, result, job_id)


def run_job(params):
    """Run runner.py and return result dict."""
    try:
        from slack_bot.runner import run
        print(f"[poller] 🚀 Running job {params.get('job_id')}", flush=True)
        result = run(params)
        return result
    except Exception as e:
        traceback.print_exc()
        return {
            "job_id":           params.get("job_id", "?"),
            "course_id":        params.get("course_id", "?"),
            "status":           "error",
            "error":            str(e),
            "slack_channel_id": params.get("slack_channel_id", ""),
            "slack_thread_ts":  params.get("slack_thread_ts", ""),
            "course_name":      None,
            "locales_checked":  [],
            "summary":          {},
            "csv_issues_s3_key": None,
            "csv_full_s3_key":  None,
            "duration_seconds": None,
        }


def _upload_csvs(s3, result, job_id):
    """Upload CSV files to S3 so Workato can download and attach to Slack."""
    for field, label in [
        ("csv_issues_path", "issues"),
        ("csv_full_path",   "full"),
    ]:
        local_path = result.get(field)
        if not local_path or not Path(local_path).exists():
            continue
        s3_key = f"course-qa/csvs/{job_id}_qa_{label}.csv"
        s3.upload_file(local_path, BUCKET, s3_key)
        # Store S3 key in result so Workato knows where to find it
        result[f"csv_{label}_s3_key"] = s3_key
        print(f"[poller] 📊 Uploaded {label} CSV → s3://{BUCKET}/{s3_key}", flush=True)


def main():
    print(f"[poller] 🔄 Starting — polling every {POLL_INTERVAL}s", flush=True)
    print(f"[poller]    Bucket: s3://{BUCKET}", flush=True)
    print(f"[poller]    Pending:  {PENDING}", flush=True)
    print(f"[poller]    Complete: {COMPLETE}", flush=True)

    s3 = boto3.client("s3")

    while True:
        try:
            poll_once(s3)
        except Exception as e:
            print(f"[poller] ⚠️  Poll error (continuing): {e}", flush=True)
            traceback.print_exc()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
