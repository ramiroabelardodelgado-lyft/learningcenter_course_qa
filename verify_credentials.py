#!/usr/bin/env python3
"""
verify_credentials.py
=====================
Tests every credential and connection for the Language QA Pipeline.

Run on the SageMaker instance:
    cd $HOME/studio
    python verify_credentials.py

Or from laptop (for Contentful/GitHub/Slack only — no S3):
    python verify_credentials.py --no-s3

Reads from $HOME/studio/.env (instance) or ./.env (laptop).
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════════════
# .env loader
# ═══════════════════════════════════════════════════════════════════════

def load_env():
    """Load .env file — try instance path first, then local."""
    candidates = [
        Path.home() / "studio" / ".env",
        Path(".env"),
    ]
    for env_path in candidates:
        if env_path.exists():
            print(f"  Loading: {env_path}")
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key == "AWS_PROFILE":
                    continue  # never set this on instance
                os.environ[key] = val
            return str(env_path)
    return None


# ═══════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════

class Results:
    def __init__(self):
        self.tests = []

    def record(self, service, test_name, passed, detail=""):
        status = "✅ PASS" if passed else "❌ FAIL"
        self.tests.append({
            "service": service,
            "test": test_name,
            "passed": passed,
            "detail": detail,
        })
        print(f"  {status}  {test_name}")
        if detail:
            for line in detail.split("\n"):
                print(f"         {line}")

    def summary(self):
        passed = sum(1 for t in self.tests if t["passed"])
        failed = sum(1 for t in self.tests if not t["passed"])
        total = len(self.tests)

        print(f"\n{'═'*60}")
        print(f"  SUMMARY: {passed}/{total} passed, {failed} failed")
        print(f"{'═'*60}")

        if failed:
            print(f"\n  Failed tests:")
            for t in self.tests:
                if not t["passed"]:
                    print(f"    ❌ [{t['service']}] {t['test']}")
                    if t["detail"]:
                        print(f"       {t['detail'][:120]}")

        return failed == 0


def http_get(url, headers=None, timeout=10):
    """Simple HTTP GET using urllib (no dependencies)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def http_post(url, data, headers=None, timeout=10):
    """Simple HTTP POST using urllib."""
    import urllib.request
    import urllib.error
    payload = json.dumps(data).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


# ═══════════════════════════════════════════════════════════════════════
# 1. CONTENTFUL
# ═══════════════════════════════════════════════════════════════════════

def test_contentful(results):
    print(f"\n{'─'*60}")
    print(f"  1. CONTENTFUL CMA")
    print(f"{'─'*60}")

    space_id = os.environ.get("CONTENTFUL_SPACE_ID", "")
    token = os.environ.get("CONTENTFUL_CMA_TOKEN", "")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    # Check env vars exist
    if not space_id:
        results.record("Contentful", "CONTENTFUL_SPACE_ID set", False, "Missing from .env")
        return
    results.record("Contentful", "CONTENTFUL_SPACE_ID set", True, f"Value: {space_id}")

    if not token:
        results.record("Contentful", "CONTENTFUL_CMA_TOKEN set", False, "Missing from .env")
        return
    results.record("Contentful", "CONTENTFUL_CMA_TOKEN set", True,
                    f"Value: {token[:12]}...{token[-4:]}")

    # Test API: get space info
    url = f"https://api.contentful.com/spaces/{space_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.contentful.management.v1+json",
    }
    status, body = http_get(url, headers)

    if status == 200:
        data = json.loads(body)
        space_name = data.get("name", "?")
        results.record("Contentful", "Authenticate to space", True,
                        f"Space: {space_name} ({space_id})")
    elif status == 401:
        results.record("Contentful", "Authenticate to space", False,
                        "401 Unauthorized — token is invalid or expired.\n"
                        "Rotate at: app.contentful.com → Settings → API keys")
        return
    else:
        results.record("Contentful", "Authenticate to space", False,
                        f"HTTP {status}: {body[:200]}")
        return

    # Test: list locales
    url = f"https://api.contentful.com/spaces/{space_id}/environments/{env_id}/locales"
    status, body = http_get(url, headers)

    if status == 200:
        data = json.loads(body)
        locales = [loc["code"] for loc in data.get("items", [])]
        results.record("Contentful", "List locales", True,
                        f"Environment: {env_id} | Locales: {', '.join(locales)}")
    else:
        results.record("Contentful", "List locales", False,
                        f"HTTP {status} on environment '{env_id}'")

    # Test: list content types (confirms full read access)
    url = (f"https://api.contentful.com/spaces/{space_id}"
           f"/environments/{env_id}/content_types?limit=5")
    status, body = http_get(url, headers)

    if status == 200:
        data = json.loads(body)
        total = data.get("total", 0)
        names = [ct.get("name", "?") for ct in data.get("items", [])[:5]]
        results.record("Contentful", "Read content types", True,
                        f"{total} total types. Sample: {', '.join(names)}")
    else:
        results.record("Contentful", "Read content types", False,
                        f"HTTP {status}")

    # Test: fetch the known course ID
    course_id = "2yQq04tUUk1H67xlZA7PLn"
    url = (f"https://api.contentful.com/spaces/{space_id}"
           f"/environments/{env_id}/entries/{course_id}")
    status, body = http_get(url, headers)

    if status == 200:
        data = json.loads(body)
        ct = data.get("sys", {}).get("contentType", {}).get("sys", {}).get("id", "?")
        results.record("Contentful", f"Fetch test entry ({course_id[:12]}...)", True,
                        f"Content type: {ct}")
    elif status == 404:
        results.record("Contentful", f"Fetch test entry ({course_id[:12]}...)", False,
                        "404 Not Found — entry ID may be wrong or deleted")
    else:
        results.record("Contentful", f"Fetch test entry ({course_id[:12]}...)", False,
                        f"HTTP {status}")


# ═══════════════════════════════════════════════════════════════════════
# 2. GITHUB
# ═══════════════════════════════════════════════════════════════════════

def test_github(results):
    print(f"\n{'─'*60}")
    print(f"  2. GITHUB")
    print(f"{'─'*60}")

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    api = os.environ.get("GITHUB_API", "https://api.github.com")

    if not token:
        results.record("GitHub", "GITHUB_TOKEN set", False, "Missing from .env")
        return
    results.record("GitHub", "GITHUB_TOKEN set", True,
                    f"Value: {token[:10]}...{token[-4:]}")

    if not repo:
        results.record("GitHub", "GITHUB_REPO set", False, "Missing from .env")
        return
    results.record("GitHub", "GITHUB_REPO set", True, f"Value: {repo}")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Test: authenticate (check token validity)
    status, body = http_get(f"{api}/user", headers)
    if status == 200:
        data = json.loads(body)
        username = data.get("login", "?")
        results.record("GitHub", "Token authentication", True,
                        f"Authenticated as: {username}")
    elif status == 401:
        results.record("GitHub", "Token authentication", False,
                        "401 Unauthorized — token invalid/expired/revoked.\n"
                        "Generate new: github.com → Settings → Developer settings → "
                        "Personal access tokens")
        return
    else:
        results.record("GitHub", "Token authentication", False,
                        f"HTTP {status}: {body[:200]}")
        return

    # Test: access repo
    status, body = http_get(f"{api}/repos/{repo}", headers)
    if status == 200:
        data = json.loads(body)
        private = "private" if data.get("private") else "public"
        results.record("GitHub", f"Access repo ({repo})", True,
                        f"Repo exists, {private}")
    elif status == 404:
        results.record("GitHub", f"Access repo ({repo})", False,
                        "404 Not Found — repo doesn't exist or token lacks access.\n"
                        f"Create at: github.com/new → name: {repo.split('/')[-1]}")
        return
    else:
        results.record("GitHub", f"Access repo ({repo})", False,
                        f"HTTP {status}")
        return

    # Test: can list issues (confirms repo scope)
    status, body = http_get(
        f"{api}/repos/{repo}/issues?state=all&per_page=1", headers)
    if status == 200:
        data = json.loads(body)
        count = len(data)
        results.record("GitHub", "List issues (repo scope)", True,
                        f"Can read issues ({count} returned)")
    else:
        results.record("GitHub", "List issues (repo scope)", False,
                        f"HTTP {status} — token may need 'repo' scope")

    # Test: can create labels (confirms write access)
    label_name = "_verify_test_"
    status, body = http_post(
        f"{api}/repos/{repo}/labels",
        {"name": label_name, "color": "cccccc"},
        headers,
    )
    if status in (200, 201):
        results.record("GitHub", "Write access (create label)", True,
                        "Can create labels — write access confirmed")
        # Clean up: delete the test label
        import urllib.request
        del_req = urllib.request.Request(
            f"{api}/repos/{repo}/labels/{label_name}",
            headers=headers, method="DELETE")
        try:
            urllib.request.urlopen(del_req, timeout=10)
        except Exception:
            pass
    elif status == 422:
        # Label already exists — still means we have write access
        results.record("GitHub", "Write access (create label)", True,
                        "Label already exists — write access confirmed")
    else:
        results.record("GitHub", "Write access (create label)", False,
                        f"HTTP {status} — may need 'repo' scope on token")


# ═══════════════════════════════════════════════════════════════════════
# 3. AWS / S3
# ═══════════════════════════════════════════════════════════════════════

def test_aws_s3(results):
    print(f"\n{'─'*60}")
    print(f"  3. AWS / S3")
    print(f"{'─'*60}")

    try:
        import boto3
        results.record("AWS", "boto3 importable", True, f"Version: {boto3.__version__}")
    except ImportError:
        results.record("AWS", "boto3 importable", False,
                        "pip install boto3 --target $HOME/persistent-packages")
        return

    # Test: get caller identity
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account = identity.get("Account", "?")
        arn = identity.get("Arn", "?")
        results.record("AWS", "STS get-caller-identity", True,
                        f"Account: {account}\nARN: {arn}")
    except Exception as e:
        results.record("AWS", "STS get-caller-identity", False,
                        f"No credentials: {e}\n"
                        "On instance: credentials come from container role automatically.\n"
                        "On laptop: run 'aws sso login --profile lyftlearnqa' first.")
        return

    # Test: S3 bucket access
    bucket = os.environ.get("S3_BUCKET", "lyft-lyftlearn-production-iad")
    try:
        s3 = boto3.client("s3")
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="course-qa/", MaxKeys=5)
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        results.record("AWS", f"S3 list bucket ({bucket})", True,
                        f"Found {len(keys)} key(s) under course-qa/\n"
                        + "\n".join(f"  {k}" for k in keys[:5]))
    except Exception as e:
        results.record("AWS", f"S3 list bucket ({bucket})", False, str(e))
        return

    # Test: S3 write
    test_key = "course-qa/_verify_test_.json"
    test_data = json.dumps({"test": True, "ts": datetime.now(timezone.utc).isoformat()})
    try:
        s3.put_object(Bucket=bucket, Key=test_key,
                      Body=test_data.encode(), ContentType="application/json")
        results.record("AWS", "S3 write", True, f"Wrote to {test_key}")
    except Exception as e:
        results.record("AWS", "S3 write", False, str(e))
        return

    # Test: S3 read back
    try:
        resp = s3.get_object(Bucket=bucket, Key=test_key)
        body = resp["Body"].read().decode()
        data = json.loads(body)
        results.record("AWS", "S3 read", True, f"Read back: {data}")
    except Exception as e:
        results.record("AWS", "S3 read", False, str(e))

    # Clean up
    try:
        s3.delete_object(Bucket=bucket, Key=test_key)
    except Exception:
        pass

    # Test: check known prefixes
    for prefix_name, prefix in [
        ("pending", "course-qa/pending/"),
        ("complete", "course-qa/complete/"),
        ("csvs", "course-qa/csvs/"),
        ("config", "course-qa/config/"),
    ]:
        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=3)
            count = resp.get("KeyCount", 0)
            results.record("AWS", f"S3 prefix: {prefix}", True,
                            f"{count} object(s)")
        except Exception as e:
            results.record("AWS", f"S3 prefix: {prefix}", False, str(e))

    # Test: check if .env backup exists in S3
    try:
        s3.head_object(Bucket=bucket, Key="course-qa/config/.env")
        results.record("AWS", "S3 .env backup exists", True,
                        "course-qa/config/.env found")
    except Exception:
        results.record("AWS", "S3 .env backup exists", False,
                        "No .env backup in S3. Run:\n"
                        f"  aws s3 cp $HOME/studio/.env s3://{bucket}/course-qa/config/.env")


# ═══════════════════════════════════════════════════════════════════════
# 4. SLACK WEBHOOK
# ═══════════════════════════════════════════════════════════════════════

def test_slack_webhook(results, send_test=False):
    print(f"\n{'─'*60}")
    print(f"  4. SLACK WEBHOOK")
    print(f"{'─'*60}")

    webhook_url = os.environ.get("WORKATO_CALLBACK_URL", "")

    if not webhook_url:
        results.record("Slack", "WORKATO_CALLBACK_URL set", False,
                        "Missing from .env")
        return
    results.record("Slack", "WORKATO_CALLBACK_URL set", True,
                    f"URL: {webhook_url[:60]}...")

    # Validate URL format
    if "hooks.slack.com/triggers/" in webhook_url:
        results.record("Slack", "URL format", True,
                        "Slack Workflow Builder webhook")
    elif "workato.com" in webhook_url:
        results.record("Slack", "URL format", True, "Workato webhook")
    elif "webhook.site" in webhook_url:
        results.record("Slack", "URL format", True,
                        "webhook.site (testing only)")
    else:
        results.record("Slack", "URL format", False,
                        f"Unrecognized webhook URL format: {webhook_url[:80]}")

    if not send_test:
        results.record("Slack", "POST test (skipped)", True,
                        "Run with --test-slack to send a real test message")
        return

    # Send a test payload
    payload = {
        "summary": "🧪 *Credential verification test*\nThis is an automated test from `verify_credentials.py`.",
        "course_name": "Verification Test",
        "csv_url": "https://github.com/ramiroabelardodelgado-lyft/lyftlearn-qa-jobs",
        "github_issue": "https://github.com/ramiroabelardodelgado-lyft/lyftlearn-qa-jobs",
        "status": "test",
    }

    status, body = http_post(webhook_url, payload)

    if status in (200, 201, 202):
        results.record("Slack", "POST test message", True,
                        f"HTTP {status} — check #learning-center-qa for the message")
    else:
        results.record("Slack", "POST test message", False,
                        f"HTTP {status}: {body[:200]}\n"
                        "The webhook URL may be expired or the Slack Workflow deleted.")


# ═══════════════════════════════════════════════════════════════════════
# 5. GOOGLE SHEETS (job queue)
# ═══════════════════════════════════════════════════════════════════════

def test_google_sheets(results):
    print(f"\n{'─'*60}")
    print(f"  5. GOOGLE SHEETS (JOB QUEUE)")
    print(f"{'─'*60}")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "1dVd4azBziNzDMq6IggAYyS3RXvM81qTeevqaqLAboGQ")

    # Try public CSV export (works if sheet is publicly readable)
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid=0"
    status, body = http_get(url)

    if status == 200 and "job_id" in body:
        lines = body.strip().split("\n")
        results.record("Sheets", "Public read access", True,
                        f"Sheet readable, {len(lines)-1} data row(s)")
    elif status == 200:
        results.record("Sheets", "Public read access", True,
                        f"Sheet readable but may be empty or have different headers")
    elif status == 302 or "accounts.google.com" in body:
        results.record("Sheets", "Public read access", False,
                        "Sheet requires auth — not publicly readable.\n"
                        "Share it: Open sheet → Share → Anyone with link → Viewer")
    else:
        results.record("Sheets", "Public read access", False,
                        f"HTTP {status}: {body[:200]}")


# ═══════════════════════════════════════════════════════════════════════
# 6. LYFT_LLM (aiproxy — instance only)
# ═══════════════════════════════════════════════════════════════════════

def test_lyft_llm(results):
    print(f"\n{'─'*60}")
    print(f"  6. LYFT_LLM (aiproxy)")
    print(f"{'─'*60}")

    try:
        import lyft_llm
        results.record("LLM", "lyft_llm importable", True,
                        f"Package found")
    except ImportError:
        results.record("LLM", "lyft_llm importable", False,
                        "Not installed. Run on instance:\n"
                        "  pip install lyft-llm --extra-index-url https://pypi.lyft.net/pypi "
                        "--target $HOME/persistent-packages")
        return

    try:
        import lyft_llm.integrations.langchain as llc
        results.record("LLM", "langchain integration", True, "Loaded")
    except ImportError as e:
        results.record("LLM", "langchain integration", False,
                        f"{e}\nInstall: pip install langchain-core langchain-aws langchain-openai "
                        "--target $HOME/persistent-packages")
        return

    # Test: initialize LLM (doesn't make a call yet)
    try:
        chat = llc.make_llm(model_id="us.anthropic.claude-3-5-sonnet-20241022-v2:0")
        results.record("LLM", "Initialize Claude via aiproxy", True,
                        "LLM client created successfully")
    except Exception as e:
        results.record("LLM", "Initialize Claude via aiproxy", False,
                        f"{e}")
        return

    # Test: actual LLM call (small, fast)
    try:
        response = chat.invoke("Reply with only the word 'ok'.")
        text = response.content if hasattr(response, 'content') else str(response)
        results.record("LLM", "LLM inference call", True,
                        f"Response: {text.strip()[:50]}")
    except Exception as e:
        results.record("LLM", "LLM inference call", False, f"{e}")


# ═══════════════════════════════════════════════════════════════════════
# 7. PIPELINE SCRIPTS
# ═══════════════════════════════════════════════════════════════════════

def test_pipeline_scripts(results):
    print(f"\n{'─'*60}")
    print(f"  7. PIPELINE SCRIPTS")
    print(f"{'─'*60}")

    studio = Path.home() / "studio"

    scripts = {
        "extract_course.py": studio / "extract_course.py",
        "language_qa.py": studio / "language_qa.py",
        "runner.py": studio / "runner.py",
        "poller.py": studio / "poller.py",
        "github_bridge.py": studio / "slack_bot" / "github_bridge.py",
    }

    for name, path in scripts.items():
        # Also check studio root if not in slack_bot/
        alt_path = studio / name
        if path.exists():
            size = path.stat().st_size
            results.record("Scripts", f"{name} exists", True,
                            f"{path} ({size:,} bytes)")
        elif alt_path.exists():
            size = alt_path.stat().st_size
            results.record("Scripts", f"{name} exists", True,
                            f"{alt_path} ({size:,} bytes)")
        else:
            results.record("Scripts", f"{name} exists", False,
                            f"Not found at {path} or {alt_path}")

    # Check persistent-packages
    pp = Path.home() / "persistent-packages"
    if pp.exists():
        pkg_count = sum(1 for _ in pp.iterdir())
        results.record("Scripts", "persistent-packages/ exists", True,
                        f"{pkg_count} items")
    else:
        results.record("Scripts", "persistent-packages/ exists", False,
                        "Missing — packages won't survive restarts.\n"
                        "  mkdir -p $HOME/persistent-packages")

    # Check PYTHONPATH
    pythonpath = os.environ.get("PYTHONPATH", "")
    if "persistent-packages" in pythonpath:
        results.record("Scripts", "PYTHONPATH includes persistent-packages", True,
                        f"PYTHONPATH={pythonpath[:80]}...")
    else:
        results.record("Scripts", "PYTHONPATH includes persistent-packages", False,
                        "Add to ~/.bashrc:\n"
                        '  export PYTHONPATH="$HOME/persistent-packages:$PYTHONPATH"')


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Verify all pipeline credentials")
    parser.add_argument("--no-s3", action="store_true",
                        help="Skip AWS/S3 tests (for laptop use)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM test (avoids making an API call)")
    parser.add_argument("--test-slack", action="store_true",
                        help="Send a real test message to Slack webhook")
    parser.add_argument("--quick", action="store_true",
                        help="Only test credentials, skip scripts/packages")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         Language QA Pipeline — Credential Verifier       ║
╚══════════════════════════════════════════════════════════╝
  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
  Host: {os.uname().nodename if hasattr(os, 'uname') else 'unknown'}
""")

    env_file = load_env()
    if env_file:
        print(f"  Loaded .env from: {env_file}\n")
    else:
        print(f"  ⚠️  No .env found — using existing environment only\n")

    results = Results()

    # Run tests
    test_contentful(results)
    test_github(results)

    if not args.no_s3:
        test_aws_s3(results)
    else:
        print(f"\n{'─'*60}")
        print(f"  3. AWS / S3 — SKIPPED (--no-s3)")
        print(f"{'─'*60}")

    test_slack_webhook(results, send_test=args.test_slack)
    test_google_sheets(results)

    if not args.no_llm and not args.no_s3:
        test_lyft_llm(results)
    else:
        print(f"\n{'─'*60}")
        print(f"  6. LYFT_LLM — SKIPPED")
        print(f"{'─'*60}")

    if not args.quick:
        test_pipeline_scripts(results)

    all_passed = results.summary()

    if all_passed:
        print(f"\n🎉 All systems go!\n")
    else:
        print(f"\n⚠️  Fix the failed tests above, then re-run.\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
