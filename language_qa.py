#!/usr/bin/env python3
"""
Language QA Checker (LLM-powered via aiproxy)
===============================================
Reads extracted course JSON and uses Claude (via Lyft aiproxy/Bedrock)
to verify every text field is in the correct language, flag escape
character artifacts, and catch other content quality issues.

Two-pass approach:
  Pass 1: Batch check all fields (fast, may have false positives)
  Pass 2: Individually verify each flagged field (precise, confirms or dismisses)

Run on LyftLearn (SageMaker) where aiproxy is available.

Usage:
    # Check all languages
    python language_qa.py --input ./output/De-escalation/

    # Check a specific locale
    python language_qa.py --input ./output/De-escalation/ --locale es

    # Save report
    python language_qa.py --input ./output/De-escalation/ --save

    # Dry run — show what would be checked without calling the LLM
    python language_qa.py --input ./output/De-escalation/ --dry-run

Dependencies (install on LyftLearn):
    pip install lyft-llm --extra-index-url https://pypi.lyft.net/pypi
    pip install langchain-core langchain-aws langchain-openai
"""

import os
import sys
import json
import csv
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone


# ═══════════════════════════════════════════════════════════════════════
# LLM Client
# ═══════════════════════════════════════════════════════════════════════

def get_llm():
    """Initialize Claude via Lyft aiproxy."""
    try:
        import lyft_llm.integrations.langchain as llc
        chat = llc.make_llm(
            model_id="us.anthropic.claude-sonnet-4-6",
            model_kwargs={"temperature": 0}
        )
        return chat
    except ImportError:
        print("❌ lyft_llm not available. This script must run on LyftLearn.")
        print("   pip install lyft-llm --extra-index-url https://pypi.lyft.net/pypi")
        print("   pip install langchain-core langchain-aws langchain-openai")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to initialize LLM: {e}")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════

LOCALE_NAMES = {
    "en-US": "English",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
}

# Max text fields per LLM call (to stay within context limits)
BATCH_SIZE = 40

# Component types to skip (not user-facing or intentionally not translated)
# - objective: internal learning objectives, not shown to drivers
# - videoEmbedded/video alt text: often left in English intentionally
SKIP_COMPONENT_TYPES = {"objective"}
SKIP_FIELD_TYPES = {"alternativeText"}  # video alt text fields


# ═══════════════════════════════════════════════════════════════════════
# Text Extraction from Course JSON
# ═══════════════════════════════════════════════════════════════════════

def extract_text_fields(course_data):
    """
    Walk the course JSON and yield every text field with its location + entry IDs.
    Yields: (field_id, path_string, text, component_type, metadata)
    metadata includes IDs needed for Contentful URLs.
    """
    field_num = 0

    for li, lesson in enumerate(course_data.get("lessons", []), 1):
        lesson_name = lesson.get("name", f"Lesson {li}")
        lesson_id = lesson.get("lesson_id", "")

        # SKIPPED: objectives are internal-only, not translated for drivers
        # for oi, obj in enumerate(lesson.get("objectives", []), 1):
        #     ...

        for ai, activity in enumerate(lesson.get("activities", []), 1):
            act_name = activity.get("name", f"Activity {ai}")
            act_id = activity.get("activity_id", "")
            act_path = f"Lesson {li}: {lesson_name} > Activity {ai}: {act_name}"

            for ci, comp in enumerate(activity.get("components", []), 1):
                ctype = comp.get("type", "?")
                comp_id = comp.get("id", "")
                comp_path = f"{act_path} > Component {ci} ({ctype})"

                meta_base = {
                    "lesson_name": lesson_name, "lesson_id": lesson_id,
                    "activity_name": act_name, "activity_id": act_id,
                    "component_id": comp_id,
                }

                if ctype == "simpleText":
                    for pi, para in enumerate(comp.get("paragraphs", []), 1):
                        if para and para.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > paragraph {pi}", para, ctype,
                                   {**meta_base, "field_name": f"paragraph{pi}"})

                elif ctype == "list":
                    title = comp.get("displayTitle", "")
                    if title and title.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > displayTitle", title, ctype,
                               {**meta_base, "field_name": "displayTitle"})
                    for ii, item in enumerate(comp.get("items", []), 1):
                        if item and item.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > item {ii}", item, ctype,
                                   {**meta_base, "field_name": f"listItem{ii}"})

                elif ctype == "fact":
                    title = comp.get("displayTitle", "")
                    if title and title.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > displayTitle", title, ctype,
                               {**meta_base, "field_name": "displayTitle"})
                    para = comp.get("paragraph", "")
                    if para and para.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > paragraph", para, ctype,
                               {**meta_base, "field_name": "paragraph"})

                elif ctype == "quote":
                    quote = comp.get("displayQuote", "")
                    if quote and quote.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > displayQuote", quote, ctype,
                               {**meta_base, "field_name": "displayQuote"})
                    source = comp.get("source")
                    if source:
                        about = source.get("about", "")
                        if about and about.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > source.about", about, ctype,
                                   {**meta_base, "field_name": "source.about"})

                elif ctype == "textAndLink":
                    for pi, para in enumerate(comp.get("paragraphs", []), 1):
                        if para and para.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > paragraph {pi}", para, ctype,
                                   {**meta_base, "field_name": f"paragraph{pi}"})
                    link = comp.get("link")
                    if link:
                        dt = link.get("displayText", "")
                        if dt and dt.strip():
                            field_num += 1
                            link_id = link.get("id", comp_id)
                            yield (f"F{field_num}", f"{comp_path} > link.displayText", dt, ctype,
                                   {**meta_base, "component_id": link_id, "field_name": "displayText"})

                elif ctype == "linkedText":
                    dt = comp.get("displayText", "")
                    if dt and dt.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > displayText", dt, ctype,
                               {**meta_base, "field_name": "displayText"})

                elif ctype == "multiChoiceQuestion":
                    q = comp.get("question", "")
                    if q and q.strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{comp_path} > question", q, ctype,
                               {**meta_base, "field_name": "displayQuestion"})
                    for ani, ans in enumerate(comp.get("answers", []), 1):
                        if ans.get("text", "").strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > answer {ani}", ans["text"], ctype,
                                   {**meta_base, "field_name": f"answer{ani}"})
                        if ans.get("info", "").strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > answerInfo {ani}", ans["info"], ctype,
                                   {**meta_base, "field_name": f"answerInfo{ani}"})

                elif ctype in ("videoEmbedded", "video"):
                    # SKIPPED: video alt text is often intentionally left in English
                    pass

                elif ctype.startswith("unknown:"):
                    for key, val in comp.get("fields", {}).items():
                        if isinstance(val, str) and val.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{comp_path} > {key}", val, ctype,
                                   {**meta_base, "field_name": key})

    # Quizzes
    for qi, quiz in enumerate(course_data.get("quizzes", []), 1):
        quiz_name = quiz.get("name", f"Quiz {qi}")
        quiz_id = quiz.get("quiz_id", "")
        quiz_path = f"Quiz {qi}: {quiz_name}"

        intro = quiz.get("intro")
        if intro:
            intro_id = intro.get("activity_id", "")
            for ci, comp in enumerate(intro.get("components", []), 1):
                ctype = comp.get("type", "?")
                comp_id = comp.get("id", "")
                if ctype == "simpleText":
                    for pi, para in enumerate(comp.get("paragraphs", []), 1):
                        if para and para.strip():
                            field_num += 1
                            yield (f"F{field_num}", f"{quiz_path} > Intro > paragraph {pi}", para, ctype, {
                                "lesson_name": quiz_name, "lesson_id": quiz_id,
                                "activity_name": "Quiz Intro", "activity_id": intro_id,
                                "component_id": comp_id, "field_name": f"paragraph{pi}",
                            })

        for qqi, question in enumerate(quiz.get("questions", []), 1):
            if question.get("type") == "multiChoiceQuestion":
                q_path = f"{quiz_path} > Question {qqi}"
                q_id = question.get("id", "")
                q_meta = {
                    "lesson_name": quiz_name, "lesson_id": quiz_id,
                    "activity_name": f"Question {qqi}", "activity_id": q_id,
                    "component_id": q_id,
                }
                q = question.get("question", "")
                if q and q.strip():
                    field_num += 1
                    yield (f"F{field_num}", f"{q_path} > question", q, "multiChoiceQuestion",
                           {**q_meta, "field_name": "displayQuestion"})
                for ani, ans in enumerate(question.get("answers", []), 1):
                    if ans.get("text", "").strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{q_path} > answer {ani}", ans["text"], "multiChoiceQuestion",
                               {**q_meta, "field_name": f"answer{ani}"})
                    if ans.get("info", "").strip():
                        field_num += 1
                        yield (f"F{field_num}", f"{q_path} > answerInfo {ani}", ans["info"], "multiChoiceQuestion",
                               {**q_meta, "field_name": f"answerInfo{ani}"})


# ═══════════════════════════════════════════════════════════════════════
# LLM QA Check — Pass 1 (batch detection)
# ═══════════════════════════════════════════════════════════════════════

def build_prompt(fields, expected_language, locale):
    """Build the QA prompt for a batch of text fields."""
    fields_block = ""
    for fid, path, text, ctype in fields:
        display_text = text[:500] + "..." if len(text) > 500 else text
        fields_block += f'\n[{fid}] """{display_text}"""\n'

    prompt = f"""You are a QA tool for a multilingual tutorial app. Your job is to check if each text field below is written in the correct language.

EXPECTED LANGUAGE: {expected_language} (locale: {locale})

For each field, check for these issues:

1. WRONG_LANGUAGE: The field AS A WHOLE is written in the wrong language.
   - Judge by the OVERALL language of the entire paragraph/sentence, not individual words.
   - Only flag if an ENTIRE FIELD or a FULL SENTENCE within the field is in the wrong language.
   - DO NOT flag individual English words, acronyms, or short phrases within an otherwise
     correctly translated field. Examples that are NEVER issues:
     * Technical terms and acronyms in any language (DMV, GPS, ETA, ADT, PIN, ID, SOS)
     * Brand names (Lyft, NOVA, Uber, Google Maps)
     * Numbers, addresses, phone numbers (911, 511)
     * Proper nouns and place names
     * Industry-specific terms that are commonly kept in English
   - Translations are handled by professional translators (Smartling/Mothertongue) who
     have specific rules about which terms to keep in English. Trust their choices.
   - Be especially careful with Spanish/Portuguese/French — they are similar but distinct.

2. ESCAPE_CHARS: The text contains backslash escape artifacts that should not appear in user-facing content.
   Look for patterns like: \\( \\) \\! \\- \\. \\,
   These are Contentful/markdown artifacts that indicate a rendering or export bug.

3. UNTRANSLATED: An ENTIRE field was never translated — the complete text is still in English.
   - Only flag if the WHOLE field is in English, not just a word or phrase.
   - This means the English source text was left completely as-is with no translation at all.
   - UNTRANSLATED means EVERY SENTENCE in the field is in English. If even one sentence is
     in {expected_language}, the field is NOT untranslated.
   - DO NOT flag a field as UNTRANSLATED just because it contains one or two English words.

IMPORTANT RULES:
- A field with 95%+ correct {expected_language} and a few English terms is a PASS.
- URLs (lft.to/..., lyft.com/..., etc.) are NEVER issues — skip them entirely.
- DO NOT check spelling, grammar, or typos — that is not your job.
- Your ONLY job is: is this field in the correct language? Yes or no.

Respond ONLY with a JSON array. For each field that has ANY issue, include an object:
{{"id": "F1", "issues": [{{"type": "WRONG_LANGUAGE", "detected_language": "English", "detail": "Entire paragraph is in English instead of {expected_language}"}}, {{"type": "ESCAPE_CHARS", "detail": "Found \\\\( and \\\\) escape sequences"}}]}}

If a field has NO issues, do NOT include it in the output.
If ALL fields pass, respond with an empty array: []

IMPORTANT: Respond with ONLY the JSON array, no markdown, no explanation, no preamble.

FIELDS TO CHECK:
{fields_block}"""

    return prompt


def check_batch(llm, fields, expected_language, locale):
    """Send a batch of fields to the LLM for checking."""
    prompt = build_prompt(fields, expected_language, locale)

    try:
        response = llm.invoke(prompt)
        response_text = response.content if hasattr(response, 'content') else str(response)

        # Clean response — strip markdown fences if present
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        issues = json.loads(cleaned)

        if not isinstance(issues, list):
            print(f"  ⚠️  Unexpected LLM response format, treating as no issues")
            return []

        return issues

    except json.JSONDecodeError as e:
        print(f"  ⚠️  Failed to parse LLM response: {e}")
        print(f"     Raw: {response_text[:300]}...")
        return []
    except Exception as e:
        print(f"  ⚠️  LLM call failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# LLM QA Check — Pass 2 (verification of flagged fields)
# ═══════════════════════════════════════════════════════════════════════

def verify_flags(llm, flagged_fields, expected_language, locale, field_lookup):
    """
    Pass 2: Re-check each flagged field individually with a stricter
    confirmation prompt. Returns only the entries that are confirmed.
    """
    if not flagged_fields:
        return []

    confirmed = []

    for entry in flagged_fields:
        fid = entry["field_id"]

        # Get full text from field_lookup (text_sample may be truncated)
        full_text = entry["text_sample"]
        if fid in field_lookup:
            full_text = field_lookup[fid][1]  # (path, text, ctype, meta)

        has_prescan_only = all(i.get("source") == "prescan" for i in entry["issues"])
        if has_prescan_only:
            # Escape chars are deterministic — always keep, no need to verify
            confirmed.append(entry)
            continue

        # Verify each LLM-flagged issue
        any_confirmed = False
        confirmed_issues = []

        for issue in entry["issues"]:
            if issue.get("source") == "prescan":
                confirmed_issues.append(issue)
                continue

            itype = issue["type"]
            detail = issue.get("detail", "")

            prompt = f"""A QA tool flagged this text field as having a language issue. Your job is to VERIFY or DISMISS the flag.

EXPECTED LANGUAGE: {expected_language} (locale: {locale})
FLAG TYPE: {itype}
FLAG DETAIL: {detail}

TEXT: \"\"\"{full_text[:800]}\"\"\"

Before deciding, consider:
- Is the ENTIRE field or a FULL SENTENCE in the wrong language? If so → CONFIRM
- Is the field clearly in {expected_language} with just a few English terms, acronyms,
  brand names, URLs, or technical words? If so → DISMISS
- Acronyms (DMV, GPS, ETA, PIN, ID), brand names (Lyft, NOVA), URLs (lft.to/...),
  and industry terms kept in English are NORMAL in professional translations.
- Spelling mistakes or typos are NOT language issues → DISMISS
- Your ONLY question: Is this field in the correct language overall? Yes = DISMISS, No = CONFIRM.

Respond with ONLY one line:
CONFIRM: reason
or
DISMISS: reason"""

            try:
                response = llm.invoke(prompt)
                response_text = response.content if hasattr(response, 'content') else str(response)
                result = response_text.strip()

                if result.upper().startswith("CONFIRM"):
                    print(f"    ✅ {fid} [{itype}] confirmed: {result}")
                    issue["verified"] = True
                    confirmed_issues.append(issue)
                    any_confirmed = True
                else:
                    print(f"    ❌ {fid} [{itype}] dismissed: {result}")

            except Exception as e:
                print(f"    ⚠️  Verify failed for {fid}: {e}")
                # On error, keep the flag (safer)
                confirmed_issues.append(issue)
                any_confirmed = True

        if any_confirmed or confirmed_issues:
            entry["issues"] = confirmed_issues
            if confirmed_issues:
                confirmed.append(entry)

    return confirmed


# ═══════════════════════════════════════════════════════════════════════
# Deterministic Pre-Scan (free, instant, always runs)
# ═══════════════════════════════════════════════════════════════════════

ESCAPE_PATTERN = re.compile(r'\\[()!,.\-\[\]]')

def prescan_field(text):
    """Quick deterministic checks — no LLM needed."""
    issues = []
    escapes = ESCAPE_PATTERN.findall(text)
    if escapes:
        unique = list(set(escapes))
        issues.append({
            "type": "ESCAPE_CHARS",
            "detail": f"Found escape artifacts: {', '.join(unique)}",
            "source": "prescan",
        })
    return issues


# ═══════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════

def run_qa(llm, course_data, locale, dry_run=False):
    """Run full QA on a single locale."""
    expected_language = LOCALE_NAMES.get(locale, locale)

    all_fields = list(extract_text_fields(course_data))
    print(f"  📝 {len(all_fields)} text fields to check")

    results = {
        "locale": locale,
        "expected_language": expected_language,
        "course_name": course_data.get("name", "?"),
        "course_id": course_data.get("course_id", "?"),
        "total_fields": len(all_fields),
        "issues": [],
        "all_fields_meta": [],  # every field with pass/fail for CSV
        "summary": {"WRONG_LANGUAGE": 0, "ESCAPE_CHARS": 0, "UNTRANSLATED": 0},
    }

    # field_lookup: fid → (path, text, ctype, metadata)
    field_lookup = {fid: (path, text, ctype, meta) for fid, path, text, ctype, meta in all_fields}

    # Store all fields for CSV (including passes)
    for fid, path, text, ctype, meta in all_fields:
        results["all_fields_meta"].append({
            "field_id": fid,
            "path": path,
            "text_sample": text[:200].replace("\n", " "),
            "component_type": ctype,
            "meta": meta,
        })

    # ── Step 1: Deterministic pre-scan ────────────────────────────────
    prescan_issues = {}
    for fid, path, text, ctype, meta in all_fields:
        field_issues = prescan_field(text)
        if field_issues:
            prescan_issues[fid] = field_issues

    if prescan_issues:
        print(f"  🔎 Pre-scan: {len(prescan_issues)} fields with escape chars")

    # ── Step 2: LLM batch check (Pass 1) ─────────────────────────────
    llm_fields = [(fid, path, text, ctype) for fid, path, text, ctype, meta in all_fields]
    llm_issues_by_id = {}
    if dry_run:
        print(f"  🏃 Dry run — skipping LLM calls")
        print(f"     Would send {len(all_fields)} fields in {(len(all_fields) + BATCH_SIZE - 1) // BATCH_SIZE} batch(es)")
    else:
        batches = [llm_fields[i:i + BATCH_SIZE] for i in range(0, len(llm_fields), BATCH_SIZE)]
        print(f"  🤖 Pass 1: Sending {len(batches)} batch(es) to Claude...")

        for bi, batch in enumerate(batches, 1):
            print(f"     Batch {bi}/{len(batches)} ({len(batch)} fields)...")
            batch_issues = check_batch(llm, batch, expected_language, locale)
            for issue in batch_issues:
                fid = issue.get("id", "")
                if fid not in llm_issues_by_id:
                    llm_issues_by_id[fid] = []
                for i in issue.get("issues", []):
                    i["source"] = "llm"
                    llm_issues_by_id[fid].append(i)

    # Merge prescan + LLM issues
    all_issue_ids = set(list(prescan_issues.keys()) + list(llm_issues_by_id.keys()))

    # Build a set of field IDs with issues for CSV status
    issue_map = {}  # fid → list of issues

    for fid in sorted(all_issue_ids, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
        if fid not in field_lookup:
            continue
        path, text, ctype, meta = field_lookup[fid]

        combined = []
        combined.extend(prescan_issues.get(fid, []))
        combined.extend(llm_issues_by_id.get(fid, []))

        seen_types = set()
        deduped = []
        for issue in combined:
            itype = issue["type"]
            if itype not in seen_types:
                seen_types.add(itype)
                deduped.append(issue)

        issue_map[fid] = deduped

        results["issues"].append({
            "field_id": fid,
            "path": path,
            "text_sample": text[:200].replace("\n", " "),
            "component_type": ctype,
            "meta": meta,
            "issues": deduped,
        })

        for issue in deduped:
            itype = issue["type"]
            if itype in results["summary"]:
                results["summary"][itype] += 1

    # ── Step 3: Verification pass (Pass 2) ───────────────────────────
    # Re-check LLM-flagged fields individually to reduce false positives
    if not dry_run and results["issues"]:
        llm_flagged = [e for e in results["issues"]
                       if any(i.get("source") == "llm" for i in e["issues"])]
        if llm_flagged:
            print(f"  🔍 Pass 2: Verifying {len(llm_flagged)} flagged field(s)...")
            confirmed = verify_flags(llm, llm_flagged, expected_language, locale, field_lookup)

            # Rebuild issues list: keep prescan-only entries + confirmed LLM entries
            prescan_only = [e for e in results["issues"]
                           if all(i.get("source") == "prescan" for i in e["issues"])]
            results["issues"] = prescan_only + confirmed

            # Rebuild issue_map and recount summary
            issue_map = {}
            results["summary"] = {"WRONG_LANGUAGE": 0, "ESCAPE_CHARS": 0, "UNTRANSLATED": 0}
            for entry in results["issues"]:
                fid = entry["field_id"]
                issue_map[fid] = entry["issues"]
                for issue in entry["issues"]:
                    itype = issue["type"]
                    if itype in results["summary"]:
                        results["summary"][itype] += 1

            print(f"  📊 After verification: {len(results['issues'])} issues remain "
                  f"(was {len(llm_flagged)} LLM + {len(prescan_only)} prescan)")

    # Annotate all_fields_meta with status
    for field_entry in results["all_fields_meta"]:
        fid = field_entry["field_id"]
        if fid in issue_map:
            types = [i["type"] for i in issue_map[fid]]
            if "WRONG_LANGUAGE" in types or "UNTRANSLATED" in types:
                field_entry["status"] = "FAIL"
            else:
                field_entry["status"] = "WARNING"
            field_entry["issue_types"] = types
            field_entry["issue_details"] = "; ".join(i.get("detail", "") for i in issue_map[fid])
        else:
            field_entry["status"] = "PASS"
            field_entry["issue_types"] = []
            field_entry["issue_details"] = ""

    return results


def print_report(results):
    """Print formatted QA report."""
    locale = results["locale"]
    summary = results["summary"]

    print(f"\n{'═'*70}")
    print(f"  LANGUAGE QA: {results['course_name']}")
    print(f"  Locale: {locale} (expected: {results['expected_language']})")
    print(f"{'═'*70}")
    print(f"\n  Fields checked:     {results['total_fields']}")
    print(f"  Fields with issues: {len(results['issues'])}")
    print(f"")
    print(f"  ❌ Wrong language:  {summary['WRONG_LANGUAGE']}")
    print(f"  ⚠️  Escape chars:   {summary['ESCAPE_CHARS']}")
    print(f"  🔄 Untranslated:   {summary['UNTRANSLATED']}")

    if not results["issues"]:
        print(f"\n  ✅ All fields passed!")
        return 0

    by_type = {"WRONG_LANGUAGE": [], "ESCAPE_CHARS": [], "UNTRANSLATED": []}
    for entry in results["issues"]:
        for issue in entry["issues"]:
            itype = issue["type"]
            if itype in by_type:
                by_type[itype].append((entry, issue))

    if by_type["WRONG_LANGUAGE"]:
        print(f"\n{'─'*70}")
        print(f"  ❌ WRONG LANGUAGE ({len(by_type['WRONG_LANGUAGE'])})")
        print(f"{'─'*70}")
        for entry, issue in by_type["WRONG_LANGUAGE"]:
            detected = issue.get("detected_language", "?")
            print(f"\n  [{entry['field_id']}] {entry['path']}")
            print(f"       Detected: {detected}")
            print(f"       Detail: {issue.get('detail', '')}")
            print(f"       Text: \"{entry['text_sample']}\"")

    if by_type["UNTRANSLATED"]:
        print(f"\n{'─'*70}")
        print(f"  🔄 UNTRANSLATED ({len(by_type['UNTRANSLATED'])})")
        print(f"{'─'*70}")
        for entry, issue in by_type["UNTRANSLATED"]:
            print(f"\n  [{entry['field_id']}] {entry['path']}")
            print(f"       Detail: {issue.get('detail', '')}")
            print(f"       Text: \"{entry['text_sample']}\"")

    if by_type["ESCAPE_CHARS"]:
        print(f"\n{'─'*70}")
        print(f"  ⚠️  ESCAPE CHARACTERS ({len(by_type['ESCAPE_CHARS'])})")
        print(f"{'─'*70}")
        for entry, issue in by_type["ESCAPE_CHARS"]:
            print(f"\n  [{entry['field_id']}] {entry['path']}")
            print(f"       {issue.get('detail', '')}")
            print(f"       Text: \"{entry['text_sample']}\"")

    return summary["WRONG_LANGUAGE"] + summary["UNTRANSLATED"]


def print_cross_locale_summary(all_results):
    """Summary table across all locales."""
    print(f"\n{'═'*70}")
    print(f"  CROSS-LOCALE SUMMARY")
    print(f"{'═'*70}")
    print(f"\n  {'Locale':<10} {'Fields':>7} {'Wrong':>7} {'Escape':>8} {'Untrans':>9} {'Status'}")
    print(f"  {'─'*55}")

    total_critical = 0
    for r in all_results:
        s = r["summary"]
        critical = s["WRONG_LANGUAGE"] + s["UNTRANSLATED"]
        total_critical += critical
        status = "✅" if critical == 0 else "❌"
        print(f"  {status} {r['locale']:<8} {r['total_fields']:>7} "
              f"{s['WRONG_LANGUAGE']:>7} {s['ESCAPE_CHARS']:>8} {s['UNTRANSLATED']:>9}")

    total_escape = sum(r["summary"]["ESCAPE_CHARS"] for r in all_results)
    print(f"\n  Critical issues (wrong language + untranslated): {total_critical}")
    print(f"  Escape character issues: {total_escape}")
    return total_critical


# ═══════════════════════════════════════════════════════════════════════
# CSV Report
# ═══════════════════════════════════════════════════════════════════════

def contentful_url(entry_id, space_id=None, env_id=None):
    """Build a Contentful web app URL for an entry."""
    if not entry_id or not space_id:
        return ""
    env_id = env_id or "master"
    return f"https://app.contentful.com/spaces/{space_id}/environments/{env_id}/entries/{entry_id}"


def generate_csv(all_results, output_path, space_id=None, env_id=None, issues_only=False):
    """
    Generate a CSV report with one row per checked field across all locales.
    If issues_only=True, only include FAIL and WARNING rows.
    """
    headers = [
        "Status",
        "Locale",
        "Course",
        "Course ID",
        "Lesson",
        "Activity",
        "Component Type",
        "Component ID",
        "Field",
        "Issue Type",
        "Issue Detail",
        "Text Sample",
        "Component URL",
    ]

    rows = []
    for result in all_results:
        locale = result["locale"]
        course_name = result["course_name"]
        course_id = result["course_id"]

        for field_entry in result["all_fields_meta"]:
            status = field_entry["status"]
            if issues_only and status == "PASS":
                continue

            meta = field_entry.get("meta", {})
            comp_id = meta.get("component_id", "")

            row = {
                "Status": status,
                "Locale": locale,
                "Course": course_name,
                "Course ID": course_id,
                "Lesson": meta.get("lesson_name", ""),
                "Activity": meta.get("activity_name", ""),
                "Component Type": field_entry["component_type"],
                "Component ID": comp_id,
                "Field": meta.get("field_name", ""),
                "Issue Type": ", ".join(field_entry.get("issue_types", [])),
                "Issue Detail": field_entry.get("issue_details", ""),
                "Text Sample": field_entry["text_sample"],
                "Component URL": contentful_url(comp_id, space_id, env_id),
            }
            rows.append(row)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    # Stats
    total = len(rows)
    fails = sum(1 for r in rows if r["Status"] == "FAIL")
    warnings = sum(1 for r in rows if r["Status"] == "WARNING")
    passes = sum(1 for r in rows if r["Status"] == "PASS")

    label = "Issues CSV" if issues_only else "Full CSV"
    print(f"\n📊 {label}: {output_path}")
    print(f"   {total} rows ({passes} pass, {fails} fail, {warnings} warning)")
    return output_path


# ═══════════════════════════════════════════════════════════════════════
# Input Loading
# ═══════════════════════════════════════════════════════════════════════

def load_courses(input_path):
    """Load course data from file or directory."""
    input_path = Path(input_path)
    courses = {}

    if input_path.is_file():
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and not data.get("course_id"):
            for key, val in data.items():
                if isinstance(val, dict) and "course_id" in val:
                    courses[key] = val
            if courses:
                return courses

        if isinstance(data, dict) and "locale" in data:
            courses[data["locale"]] = data
            return courses

    elif input_path.is_dir():
        for f in input_path.glob("*_all_languages.json"):
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for key, val in data.items():
                if isinstance(val, dict) and "course_id" in val:
                    courses[key] = val
            if courses:
                print(f"  📂 Loaded {len(courses)} locales from {f.name}")
                return courses

        for f in sorted(input_path.glob("*.json")):
            if "all_languages" in f.name or "language_qa" in f.name:
                continue
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "locale" in data:
                courses[data["locale"]] = data

        if courses:
            print(f"  📂 Loaded {len(courses)} locale files from {input_path}")

    return courses


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Language QA — LLM-powered verification of translated course content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python language_qa.py --input ./output/De-escalation/
    python language_qa.py --input ./output/De-escalation/ --locale es
    python language_qa.py --input ./output/De-escalation/ --save --csv
    python language_qa.py --input ./output/De-escalation/ --dry-run --csv
    python language_qa.py --input ./output/De-escalation/ --skip-en --csv --save
        """,
    )
    parser.add_argument("--input", "-i", required=True, help="JSON file or directory")
    parser.add_argument("--save", "-s", action="store_true", help="Save JSON report")
    parser.add_argument("--csv", action="store_true", help="Generate CSV report (for Google Sheets)")
    parser.add_argument("--locale", "-l", default=None, help="Check only a specific locale")
    parser.add_argument("--output", "-o", default=None, help="Output dir for reports")
    parser.add_argument("--dry-run", action="store_true", help="Show fields without calling LLM (still generates CSV with pre-scan results)")
    parser.add_argument("--skip-en", action="store_true", help="Skip English locale (source language)")

    args = parser.parse_args()

    # Try to load Contentful config for URLs (optional)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    space_id = os.environ.get("CONTENTFUL_SPACE_ID")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    courses = load_courses(args.input)
    if not courses:
        print("❌ No course data found")
        sys.exit(1)

    if args.locale:
        if args.locale in courses:
            courses = {args.locale: courses[args.locale]}
        else:
            print(f"❌ Locale '{args.locale}' not found. Available: {list(courses.keys())}")
            sys.exit(1)

    if args.skip_en:
        courses = {k: v for k, v in courses.items() if k not in ("en-US", "en")}

    # Initialize LLM
    llm = None
    if not args.dry_run:
        print("🤖 Initializing Claude via aiproxy...")
        llm = get_llm()
        print("  ✅ Connected\n")

    # Run QA
    all_results = []
    for locale in sorted(courses.keys()):
        course_data = courses[locale]
        print(f"\n🔍 Checking locale: {locale} ({LOCALE_NAMES.get(locale, locale)})")
        results = run_qa(llm, course_data, locale, dry_run=args.dry_run)
        all_results.append(results)
        print_report(results)

    total_critical = print_cross_locale_summary(all_results)

    # Determine output directory
    input_path = Path(args.input)
    out_dir = Path(args.output) if args.output else (input_path if input_path.is_dir() else input_path.parent)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Get course_id for filenames (stable, unlike names which get localized)
    first_result = all_results[0] if all_results else {}
    course_id = first_result.get("course_id", "unknown")

    # Save JSON report
    if args.save:
        report = {
            "run_date": datetime.now(timezone.utc).isoformat(),
            "input": str(args.input),
            "course_id": course_id,
            "total_critical": total_critical,
            "locales": {r["locale"]: {k: v for k, v in r.items() if k != "all_fields_meta"} for r in all_results},
        }
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = out_dir / f"{course_id}_qa_{timestamp}.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n💾 JSON report: {report_path}")

    # Generate CSV
    if args.csv:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Full report (every field)
        csv_path = out_dir / f"{course_id}_qa_{timestamp}.csv"
        generate_csv(all_results, csv_path, space_id=space_id, env_id=env_id)

        # Issues only (FAIL + WARNING, skip PASS)
        csv_issues_path = out_dir / f"{course_id}_qa_{timestamp}_issues.csv"
        generate_csv(all_results, csv_issues_path, space_id=space_id, env_id=env_id, issues_only=True)

    if total_critical > 0:
        print(f"\n❌ {total_critical} critical issue(s) found")
        sys.exit(1)
    else:
        print(f"\n✅ All language checks passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()