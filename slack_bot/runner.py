#!/usr/bin/env python3
"""
Pipeline entry point — runs on the LyftLearn instance.

Works for both execution paths:
  Path A: invoked as SageMaker Processing Job entry point (reads env vars)
  Path B: called from http_server.py with params dict

All roadblock fixes are baked in — see roadblocks.md for context.
"""
import os
import sys
import json
import uuid
import traceback
from pathlib import Path
from datetime import datetime, timezone


def _load_env():
    """
    Load $HOME/studio/.env without overriding existing env vars.
    Explicitly skips AWS_PROFILE — it breaks container credentials.
    (Roadblock #7)
    """
    env_path = _studio / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        # dotenv may have set AWS_PROFILE — remove it
        os.environ.pop("AWS_PROFILE", None)
    except ImportError:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key == "AWS_PROFILE":
                continue  # always skip (Roadblock #7)
            if key not in os.environ:
                os.environ[key] = val.strip().strip('"').strip("'")


def _get(params, key, env_key, default=None, required=False):
    """Get a value from params dict (Path B) or env vars (Path A)."""
    if params and key in params and params[key] is not None:
        return str(params[key])
    val = os.environ.get(env_key, default)
    if required and not val:
        raise ValueError(f"Required parameter '{key}' / env var '{env_key}' not set")
    return val


def _post_callback(url, payload):
    """POST callback to Workato. Best-effort — never raises."""
    if not url:
        print("\nℹ️  No WORKATO_CALLBACK_URL — local test mode. Payload:")
        print(json.dumps(payload, indent=2))
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"✅ Callback sent — HTTP {r.status}")
    except Exception as e:
        print(f"⚠️  Callback failed (non-fatal): {e}")


def run(params=None):
    """
    Run the full extract + QA pipeline.

    params: optional dict for Path B (http_server.py calls this).
            Keys: job_id, course_id, languages, qa_locale, qa_skip_en,
                  workato_callback_url, slack_channel_id, slack_thread_ts
    If params is None, reads everything from environment variables (Path A).
    Returns the callback dict (useful for Path B to forward to Workato).
    """
    _load_env()

    job_id           = _get(params, "job_id",              "JOB_ID",               str(uuid.uuid4())[:8])
    course_id        = _get(params, "course_id",           "COURSE_ID",            required=True)
    languages        = _get(params, "languages",           "LANGUAGES",            "all")
    qa_locale        = _get(params, "qa_locale",           "QA_LOCALE")            or None
    qa_skip_en       = _get(params, "qa_skip_en",          "QA_SKIP_EN",           "false").lower() == "true"
    # Results returned via GitHub — Workato callback disabled for now.
    # callback_url = _get(params, "workato_callback_url", "WORKATO_CALLBACK_URL", "")
    callback_url = ""
    slack_channel_id = _get(params, "slack_channel_id",    "SLACK_CHANNEL_ID",     "")
    slack_thread_ts  = _get(params, "slack_thread_ts",     "SLACK_THREAD_TS",      "")

    # Output to EFS via symlink — use Path.home() not ~ (Roadblocks #8, #11)
    output_base = _studio / "output"

    print(f"\n{'='*60}")
    print(f"  Course QA Job: {job_id}")
    print(f"  Course:        {course_id}")
    print(f"  Languages:     {languages}")
    print(f"  Skip EN:       {qa_skip_en}")
    print(f"  QA locale:     {qa_locale or 'all'}")
    print(f"  Output base:   {output_base}")
    print(f"{'='*60}\n")

    started_at = datetime.now(timezone.utc)

    callback = {
        "job_id":           job_id,
        "course_id":        course_id,
        "status":           "error",
        "course_name":      None,
        "slack_channel_id": slack_channel_id,
        "slack_thread_ts":  slack_thread_ts,
        "locales_checked":  [],
        "summary":          {},
        "locales":          {},
        "csv_issues_path":  None,
        "csv_full_path":    None,
        "duration_seconds": None,
        "error":            None,
    }

    try:
        from extract_course import ContentfulClient, CourseExtractor
        from language_qa import run_qa, generate_csv, print_report, \
                                 print_cross_locale_summary, get_llm

        space_id  = os.environ.get("CONTENTFUL_SPACE_ID")  or (_ for _ in ()).throw(ValueError("CONTENTFUL_SPACE_ID not set"))
        cma_token = os.environ.get("CONTENTFUL_CMA_TOKEN") or (_ for _ in ()).throw(ValueError("CONTENTFUL_CMA_TOKEN not set"))
        env_id    = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

        # ── Phase 1: Extract ──────────────────────────────────────────
        print("📥 Phase 1: Extracting course content...\n")
        client    = ContentfulClient(space_id, cma_token, env_id)
        extractor = CourseExtractor(client)
        extractor.discover_locales()

        locales = None
        if languages and languages != "all":
            locales = [l.strip() for l in languages.split(",")]

        courses = extractor.extract_all_locales(course_id, locales)
        if not courses:
            raise ValueError(
                f"No data for course '{course_id}'. "
                "Course IDs are case-sensitive (Roadblock #10). "
                "Check CONTENTFUL_* env vars are set."
            )

        first_locale = next(iter(courses))
        course_name  = courses[first_locale].get("name", course_id)
        callback["course_name"] = course_name

        # safe_name matches extract_course.py output folder naming
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in course_name
        ).strip()
        out_dir = output_base / safe_name
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n✅ Extracted {len(courses)} locale(s): {', '.join(courses)}")

        # ── Phase 2: Language QA ──────────────────────────────────────
        print("\n🔍 Phase 2: Running language QA...\n")
        llm = get_llm()

        qa_results = []
        for locale, course_data in courses.items():
            if qa_skip_en and locale in ("en", "en-US"):
                print(f"  ⏭️  Skipping {locale} (source language)")
                continue
            if qa_locale and locale != qa_locale:
                continue
            print(f"\n  Checking {locale}...")
            result = run_qa(llm, course_data, locale)
            print_report(result)
            qa_results.append(result)

        if not qa_results:
            raise ValueError("No locales were QA'd — check locale / skip-en params")

        print_cross_locale_summary(qa_results)

        # ── Phase 3: Write CSVs ───────────────────────────────────────
        print("\n📄 Phase 3: Writing CSVs...\n")
        full_path   = str(out_dir / f"{job_id}_qa_full.csv")
        issues_path = str(out_dir / f"{job_id}_qa_issues.csv")

        generate_csv(qa_results, full_path, space_id=space_id, env_id=env_id)
        generate_csv(
            qa_results, issues_path, space_id=space_id, env_id=env_id, issues_only=True
        )

        print(f"  ✅ Full:   {full_path}")
        print(f"  ✅ Issues: {issues_path}")

        callback.update({
            "status":          "success",
            "locales_checked": [r["locale"] for r in qa_results],
            "summary":         {r["locale"]: r["summary"] for r in qa_results},
            "locales":         {
                r["locale"]: {
                    "course_name":   r["course_name"],
                    "total_fields":  r["total_fields"],
                    "summary":       r["summary"],
                }
                for r in qa_results
            },
            "csv_issues_path": issues_path,
            "csv_full_path":   full_path,
        })

    except Exception as exc:
        print(f"\n❌ Job failed: {exc}")
        traceback.print_exc()
        callback["error"] = str(exc)

    finally:
        duration = round(
            (datetime.now(timezone.utc) - started_at).total_seconds(), 1
        )
        callback["duration_seconds"] = duration
        print(f"\n⏱️  Duration: {duration}s")
        _post_callback(callback_url, callback)

    return callback


if __name__ == "__main__":
    run()