#!/usr/bin/env python3
"""
Lyft Tutorial Content Extractor (Self-Contained)
==================================================
Extracts course content from Contentful CMA based on the data model:

    courseContainer
      └── course (via defaultCourse or courseVariants[])
            ├── lesson (via lessons[])
            │     └── activity* (via lessonActivities[])
            │           activityMultiple:    component1, component2, component3
            │           activitySingle:      component (singular)
            │           activityDriverGuide: component1, component2
            │
            ├── quiz (via lessons[] — quiz is a lesson peer)
            │     ├── activityQuizIntro (via activityQuizIntroPage)
            │     └── componentMultiChoiceQuestion (via quizActivities[])
            │
            └── criteria (via targetingCriteria)

Component types (11 total):
    componentSimpleText        paragraph1-5
    componentVideoEmbedded     muxIdLightMode/DarkMode, isLooped
    componentVideo             muxId (single), coverImage
    componentImage             imageAssetContentful, darkModeVersion
    componentContentfulImage   image (single asset)
    componentList              listItem1-6, listType, displayTitle
    componentFact              displayTitle, paragraph, image
    componentQuote             displayQuote → contentSource
    componentTextAndLink       paragraph1-3 → componentLinkedText
    componentLinkedText        displayText, destinationUrl
    componentMultiChoiceQuestion  displayQuestion, answer1-4, correctAnswer

Usage:
    python extract_course.py --course 2yQq04tUUk1H67xlZA7PLn
    python extract_course.py --course ID1 --course ID2 --name "My Course"
    python extract_course.py --course ID1 --languages en-US,es
    python extract_course.py --course dummy --list-courses

Dependencies:
    pip install requests python-dotenv
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests


# ═══════════════════════════════════════════════════════════════════════
# Contentful CMA Client
# ═══════════════════════════════════════════════════════════════════════

class ContentfulClient:
    BASE_URL = "https://api.contentful.com"

    def __init__(self, space_id, cma_token, environment_id="master"):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cma_token}",
            "Content-Type": "application/vnd.contentful.management.v1+json",
        })
        self._base = f"{self.BASE_URL}/spaces/{space_id}/environments/{environment_id}"

    def get_entry(self, entry_id):
        return self._get(f"{self._base}/entries/{entry_id}")

    def get_entries_by_ids(self, entry_ids):
        if not entry_ids:
            return []
        resp = self._get(
            f"{self._base}/entries",
            params={"sys.id[in]": ",".join(entry_ids), "limit": min(len(entry_ids), 100)},
        )
        return resp.get("items", [])

    def get_entries_by_type(self, content_type, limit=100):
        resp = self._get(
            f"{self._base}/entries",
            params={"content_type": content_type, "limit": limit},
        )
        return resp.get("items", [])

    def get_asset(self, asset_id):
        return self._get(f"{self._base}/assets/{asset_id}")

    def get_locales(self):
        resp = self._get(f"{self._base}/locales")
        return resp.get("items", [])

    def _get(self, url, params=None, retries=3):
        for attempt in range(retries):
            resp = self.session.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = int(resp.headers.get("X-Contentful-RateLimit-Reset", 2))
                print(f"  ⏳ Rate limited, waiting {wait}s...")
                time.sleep(wait)
            elif resp.status_code == 404:
                return {}
            else:
                print(f"  ⚠️ HTTP {resp.status_code}: {url}")
                return {}
        return {}


# ═══════════════════════════════════════════════════════════════════════
# Extractor — follows the real Contentful model
# ═══════════════════════════════════════════════════════════════════════

class CourseExtractor:

    def __init__(self, client):
        self.client = client
        self._entry_cache = {}
        self._asset_cache = {}
        self._default_locale = "en-US"
        self._available_locales = []

    def discover_locales(self):
        locales = self.client.get_locales()
        self._available_locales = [loc["code"] for loc in locales]
        for loc in locales:
            if loc.get("default"):
                self._default_locale = loc["code"]
        print(f"🌐 Locales: {', '.join(self._available_locales)} (default: {self._default_locale})")
        return self._available_locales

    # ── Entry Point ───────────────────────────────────────────────────

    def extract(self, entry_id, locale):
        """
        Extract from any entry point — auto-detects if it's a
        courseContainer or a course and routes accordingly.
        """
        entry = self._get_entry(entry_id)
        if not entry:
            print(f"❌ Entry {entry_id} not found")
            return None

        ct = self._ct(entry)
        print(f"\n📚 [{locale}] Entry {entry_id} is type: {ct}")

        if ct == "courseContainer":
            return self._extract_container(entry, entry_id, locale)
        elif ct == "course":
            return self._extract_course(entry, entry_id, locale)
        else:
            print(f"⚠️ Unknown entry type: {ct}. Trying as course...")
            return self._extract_course(entry, entry_id, locale)

    def extract_all_locales(self, entry_id, locales=None):
        if not self._available_locales:
            self.discover_locales()
        locales = locales or self._available_locales
        results = {}
        for locale in locales:
            try:
                data = self.extract(entry_id, locale)
                if data:
                    results[locale] = data
            except Exception as e:
                print(f"  ❌ {locale} failed: {e}")
        return results

    # ── courseContainer ────────────────────────────────────────────────

    def _extract_container(self, entry, container_id, locale):
        fields = entry.get("fields", {})
        name = self._field(fields, "displayName", locale) or \
               self._field(fields, "internalName", locale) or container_id

        print(f"  📦 Container: {name}")

        # Route to course — try defaultCourse first, then courseVariants
        course_entry = None

        # Single default course
        default_ref = self._field(fields, "defaultCourse", locale)
        if isinstance(default_ref, dict) and "sys" in default_ref:
            course_entry = self._get_entry(default_ref["sys"]["id"])

        # Course variants array
        if not course_entry:
            variants = self._field(fields, "courseVariants", locale)
            if isinstance(variants, list) and variants:
                # Use first variant (could extend to extract all)
                first = variants[0]
                if isinstance(first, dict) and "sys" in first:
                    course_entry = self._get_entry(first["sys"]["id"])

        # Fallback: scan all fields for any Entry link
        if not course_entry:
            for fname in fields:
                val = self._field(fields, fname, locale)
                if isinstance(val, dict) and "sys" in val:
                    link_type = val["sys"].get("linkType", "")
                    if link_type == "Entry":
                        candidate = self._get_entry(val["sys"]["id"])
                        if self._ct(candidate) == "course":
                            course_entry = candidate
                            break
                elif isinstance(val, list) and val:
                    if isinstance(val[0], dict) and "sys" in val[0]:
                        candidate = self._get_entry(val[0]["sys"]["id"])
                        if self._ct(candidate) == "course":
                            course_entry = candidate
                            break

        if not course_entry:
            print(f"  ⚠️ No course found inside container")
            return {"course_id": container_id, "name": name, "locale": locale,
                    "lessons": [], "quizzes": []}

        return self._extract_course(course_entry, container_id, locale, container_name=name)

    # ── course ────────────────────────────────────────────────────────

    def _extract_course(self, entry, original_id, locale, container_name=None):
        fields = entry.get("fields", {})
        name = container_name or \
               self._field(fields, "displayName", locale) or \
               self._field(fields, "internalName", locale) or original_id

        course = {
            "course_id": original_id,
            "course_entry_id": entry["sys"]["id"],
            "name": name,
            "locale": locale,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "lessons": [],
            "quizzes": [],
        }

        # Extract lessons
        lesson_refs = self._field(fields, "lessons", locale)
        if isinstance(lesson_refs, list):
            lesson_ids = [r["sys"]["id"] for r in lesson_refs if isinstance(r, dict) and "sys" in r]
            lesson_entries = self._get_entries_batch(lesson_ids)
            for i, le in enumerate(lesson_entries):
                lesson = self._extract_lesson(le, locale, i + 1)
                course["lessons"].append(lesson)

        # Extract quizzes (scan for quiz-type children)
        for fname in fields:
            val = self._field(fields, fname, locale)
            # Single quiz link
            if isinstance(val, dict) and "sys" in val and val["sys"].get("linkType") == "Entry":
                qe = self._get_entry(val["sys"]["id"])
                if self._ct(qe) == "quiz":
                    course["quizzes"].append(self._extract_quiz(qe, locale))
            # Array of quizzes
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and "sys" in item:
                        qe = self._get_entry(item["sys"]["id"])
                        if self._ct(qe) == "quiz":
                            course["quizzes"].append(self._extract_quiz(qe, locale))

        # Stats
        course["stats"] = {
            "total_lessons": len(course["lessons"]),
            "total_activities": sum(len(l["activities"]) for l in course["lessons"]),
            "total_quizzes": len(course["quizzes"]),
            "total_quiz_questions": sum(len(q.get("questions", [])) for q in course["quizzes"]),
        }

        print(f"  ✅ {course['stats']['total_lessons']} lessons, "
              f"{course['stats']['total_activities']} activities, "
              f"{course['stats']['total_quizzes']} quizzes")

        return course

    # ── lesson ────────────────────────────────────────────────────────

    def _extract_lesson(self, entry, locale, index):
        fields = entry.get("fields", {})
        name = self._field(fields, "displayName", locale) or \
               self._field(fields, "internalName", locale) or f"Lesson {index}"

        lesson = {
            "lesson_id": entry["sys"]["id"],
            "name": name,
            "objectives": self._collect_numbered(fields, "objective", locale),
            "activities": [],
        }

        # lessonActivities[]
        act_refs = self._field(fields, "lessonActivities", locale)
        if isinstance(act_refs, list):
            act_ids = [r["sys"]["id"] for r in act_refs if isinstance(r, dict) and "sys" in r]
            act_entries = self._get_entries_batch(act_ids)
            for ae in act_entries:
                activity = self._extract_activity(ae, locale)
                lesson["activities"].append(activity)

        print(f"    📖 Lesson {index}: {name} ({len(lesson['activities'])} activities)")
        return lesson

    # ── Activities: activityMultiple, activitySingle, activityDriverGuide, activityQuizIntro

    def _extract_activity(self, entry, locale):
        fields = entry.get("fields", {})
        ct = self._ct(entry)
        name = self._field(fields, "displayName", locale) or \
               self._field(fields, "internalName", locale) or ""

        activity = {
            "activity_id": entry["sys"]["id"],
            "activity_type": ct,
            "name": name,
            "components": [],
        }

        if ct == "activitySingle":
            # activitySingle uses singular 'component' (not numbered)
            comp_ref = self._field(fields, "component", locale)
            if isinstance(comp_ref, dict) and "sys" in comp_ref:
                ce = self._get_entry(comp_ref["sys"]["id"])
                if ce:
                    component = self._extract_component(ce, locale)
                    if component:
                        activity["components"].append(component)
        else:
            # activityMultiple, activityDriverGuide, activityQuizIntro
            # all use numbered: component1, component2, component3
            comp_entries = self._resolve_numbered_links(fields, "component", locale)
            for ce in comp_entries:
                component = self._extract_component(ce, locale)
                if component:
                    activity["components"].append(component)

        return activity

    # ── quiz ──────────────────────────────────────────────────────────

    def _extract_quiz(self, entry, locale):
        fields = entry.get("fields", {})
        name = self._field(fields, "displayName", locale) or \
               self._field(fields, "internalName", locale) or "Quiz"
        min_score = self._field(fields, "minQuizScore", locale)

        quiz = {
            "quiz_id": entry["sys"]["id"],
            "name": name,
            "min_score": min_score,
            "intro": None,
            "questions": [],
        }

        # Quiz intro page
        intro_ref = self._field(fields, "activityQuizIntroPage", locale)
        if isinstance(intro_ref, dict) and "sys" in intro_ref:
            intro_entry = self._get_entry(intro_ref["sys"]["id"])
            if intro_entry:
                quiz["intro"] = self._extract_activity(intro_entry, locale)

        # Quiz activities (questions)
        qa_refs = self._field(fields, "quizActivities", locale)
        if isinstance(qa_refs, list):
            qa_ids = [r["sys"]["id"] for r in qa_refs if isinstance(r, dict) and "sys" in r]
            qa_entries = self._get_entries_batch(qa_ids)
            for qe in qa_entries:
                ct = self._ct(qe)
                if ct == "componentMultiChoiceQuestion":
                    quiz["questions"].append(self._extract_multi_choice(qe, locale))
                else:
                    # Could be another activity type
                    quiz["questions"].append(self._extract_activity(qe, locale))

        print(f"    📝 Quiz: {name} ({len(quiz['questions'])} questions)")
        return quiz

    # ═══════════════════════════════════════════════════════════════════
    # Component Parsers — one per content type
    # ═══════════════════════════════════════════════════════════════════

    def _extract_component(self, entry, locale):
        """Route to the correct parser based on content type."""
        ct = self._ct(entry)
        parsers = {
            "componentSimpleText": self._parse_simple_text,
            "componentVideoEmbedded": self._parse_video_embedded,
            "componentVideo": self._parse_video,
            "componentImage": self._parse_image,
            "componentContentfulImage": self._parse_contentful_image,
            "componentList": self._parse_list,
            "componentFact": self._parse_fact,
            "componentQuote": self._parse_quote,
            "componentTextAndLink": self._parse_text_and_link,
            "componentLinkedText": self._parse_linked_text,
            "componentMultiChoiceQuestion": self._extract_multi_choice,
            # Non-content types — skip silently
            "subjectStyling": lambda e, l: None,
            "criteria": lambda e, l: None,
            "contentSource": lambda e, l: None,  # parsed inline by componentQuote
            "learningCenterVideoMuxTest": lambda e, l: None,
            "learningCenterVideoMuxTest2": lambda e, l: None,
            "faketest": lambda e, l: None,
        }
        parser = parsers.get(ct)
        if parser:
            return parser(entry, locale)
        else:
            # Unknown component — dump what we can
            return self._parse_unknown(entry, locale)

    def _parse_simple_text(self, entry, locale):
        fields = entry.get("fields", {})
        paragraphs = self._collect_numbered(fields, "paragraph", locale)
        return {
            "type": "simpleText",
            "id": entry["sys"]["id"],
            "paragraphs": paragraphs,
        }

    def _parse_video_embedded(self, entry, locale):
        fields = entry.get("fields", {})
        return {
            "type": "videoEmbedded",
            "id": entry["sys"]["id"],
            "internalTitle": self._field(fields, "internalTitle", locale) or "",
            "muxIdLightMode": self._field(fields, "muxIdLightMode", locale) or "",
            "muxIdDarkMode": self._field(fields, "muxIdDarkMode", locale) or "",
            "isLooped": self._field(fields, "isLooped", locale) or False,
            "videoDuration": self._field(fields, "videoDuration", locale) or "",
        }

    def _parse_video(self, entry, locale):
        fields = entry.get("fields", {})
        cover_url = self._resolve_asset_url(fields, "videoCoverImageContentful", locale)
        return {
            "type": "video",
            "id": entry["sys"]["id"],
            "internalTitle": self._field(fields, "internalTitle", locale) or "",
            "muxId": self._field(fields, "muxId", locale) or "",
            "videoDuration": self._field(fields, "videoDuration", locale) or "",
            "coverImageUrl": cover_url,
        }

    def _parse_image(self, entry, locale):
        fields = entry.get("fields", {})
        light_url = self._resolve_asset_url(fields, "imageAssetContentful", locale)
        dark_url = self._resolve_asset_url(fields, "darkModeVersionOfImageContentful", locale)
        return {
            "type": "image",
            "id": entry["sys"]["id"],
            "internalName": self._field(fields, "internalName", locale) or "",
            "alternativeText": self._field(fields, "alternativeText", locale) or "",
            "lightModeUrl": light_url,
            "darkModeUrl": dark_url,
        }

    def _parse_contentful_image(self, entry, locale):
        """componentContentfulImage — simpler image, just a single asset link."""
        fields = entry.get("fields", {})
        image_url = self._resolve_asset_url(fields, "image", locale)
        return {
            "type": "contentfulImage",
            "id": entry["sys"]["id"],
            "imageUrl": image_url,
        }

    def _parse_list(self, entry, locale):
        fields = entry.get("fields", {})
        items = self._collect_numbered(fields, "listItem", locale)
        return {
            "type": "list",
            "id": entry["sys"]["id"],
            "displayTitle": self._field(fields, "displayTitle", locale) or "",
            "listType": self._field(fields, "listType", locale) or "",
            "items": items,
        }

    def _parse_fact(self, entry, locale):
        fields = entry.get("fields", {})
        image_url = self._resolve_asset_url(fields, "image", locale)
        return {
            "type": "fact",
            "id": entry["sys"]["id"],
            "displayTitle": self._field(fields, "displayTitle", locale) or "",
            "paragraph": self._field(fields, "paragraph", locale) or "",
            "imageUrl": image_url,
        }

    def _parse_quote(self, entry, locale):
        """componentQuote — quote text + source (contentSource entry)."""
        fields = entry.get("fields", {})
        quote_text = self._field(fields, "displayQuote", locale) or ""

        source_data = None
        source_ref = self._field(fields, "quoteSource", locale)
        if isinstance(source_ref, dict) and "sys" in source_ref:
            source_entry = self._get_entry(source_ref["sys"]["id"])
            if source_entry:
                sf = source_entry.get("fields", {})
                source_data = {
                    "name": self._field(sf, "displayName", locale) or "",
                    "about": self._field(sf, "displayAbout", locale) or "",
                    "imageUrl": self._resolve_asset_url(sf, "sourceImageContentful", locale),
                }

        return {
            "type": "quote",
            "id": entry["sys"]["id"],
            "displayQuote": quote_text,
            "source": source_data,
        }

    def _parse_text_and_link(self, entry, locale):
        fields = entry.get("fields", {})
        paragraphs = self._collect_numbered(fields, "paragraph", locale)

        # linkText is a reference to componentLinkedText
        link_data = None
        link_ref = self._field(fields, "linkText", locale)
        if isinstance(link_ref, dict) and "sys" in link_ref:
            link_entry = self._get_entry(link_ref["sys"]["id"])
            if link_entry:
                link_data = self._parse_linked_text(link_entry, locale)

        return {
            "type": "textAndLink",
            "id": entry["sys"]["id"],
            "paragraphs": paragraphs,
            "link": link_data,
        }

    def _parse_linked_text(self, entry, locale):
        fields = entry.get("fields", {})
        return {
            "type": "linkedText",
            "id": entry["sys"]["id"],
            "displayText": self._field(fields, "displayText", locale) or "",
            "destinationUrl": self._field(fields, "destinationUrl", locale) or "",
        }

    def _extract_multi_choice(self, entry, locale):
        fields = entry.get("fields", {})
        answers = []
        for i in range(1, 10):  # up to answer9
            answer = self._field(fields, f"answer{i}", locale)
            if answer is None:
                break
            info = self._field(fields, f"answerInfo{i}", locale) or ""
            answers.append({"text": answer, "info": info})

        # questionImage is a Link<Entry> → componentImage
        question_image = None
        qi_ref = self._field(fields, "questionImage", locale)
        if isinstance(qi_ref, dict) and "sys" in qi_ref:
            qi_entry = self._get_entry(qi_ref["sys"]["id"])
            if qi_entry:
                question_image = self._parse_image(qi_entry, locale)

        # optionalImageContentful is a direct Link<Asset>
        optional_image_url = self._resolve_asset_url(fields, "optionalImageContentful", locale)

        return {
            "type": "multiChoiceQuestion",
            "id": entry["sys"]["id"],
            "internalName": self._field(fields, "internalName", locale) or "",
            "question": self._field(fields, "displayQuestion", locale) or "",
            "correctAnswer": self._field(fields, "correctAnswer", locale),
            "showAnswer": self._field(fields, "isShowAnswerDisplayed", locale) or False,
            "answers": answers,
            "questionImage": question_image,
            "optionalImageUrl": optional_image_url,
        }

    def _parse_unknown(self, entry, locale):
        """Fallback: dump all string fields."""
        fields = entry.get("fields", {})
        ct = self._ct(entry)
        texts = {}
        for fname in fields:
            val = self._field(fields, fname, locale)
            if isinstance(val, str) and val.strip():
                texts[fname] = val
        return {
            "type": f"unknown:{ct}",
            "id": entry["sys"]["id"],
            "fields": texts,
        }

    # ═══════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════

    def _field(self, fields, name, locale):
        """Get a field value for a specific locale. CMA returns all locales."""
        val = fields.get(name)
        if not isinstance(val, dict):
            return val
        if locale in val:
            return val[locale]
        if self._default_locale in val:
            return val[self._default_locale]
        return next(iter(val.values()), None) if val else None

    def _ct(self, entry):
        """Get content type ID from entry."""
        return entry.get("sys", {}).get("contentType", {}).get("sys", {}).get("id", "unknown")

    def _collect_numbered(self, fields, prefix, locale):
        """Collect paragraph1, paragraph2, ... or listItem1, listItem2, ... etc."""
        items = []
        for i in range(1, 20):  # generous upper bound
            val = self._field(fields, f"{prefix}{i}", locale)
            if val is None:
                break
            if isinstance(val, str) and val.strip():
                items.append(val)
        return items

    def _resolve_numbered_links(self, fields, prefix, locale):
        """Resolve component1, component2, component3, ... entry links."""
        entries = []
        for i in range(1, 20):
            ref = self._field(fields, f"{prefix}{i}", locale)
            if ref is None:
                break
            if isinstance(ref, dict) and "sys" in ref:
                entry = self._get_entry(ref["sys"]["id"])
                if entry and "sys" in entry:
                    entries.append(entry)
        return entries

    def _resolve_asset_url(self, fields, field_name, locale):
        """Resolve a Link<Asset> field to its URL."""
        ref = self._field(fields, field_name, locale)
        if isinstance(ref, dict) and "sys" in ref:
            asset = self._get_asset(ref["sys"]["id"])
            if asset:
                file_data = asset.get("fields", {}).get("file", {})
                # CMA: file is locale-keyed
                if isinstance(file_data, dict):
                    file_obj = file_data.get(locale) or \
                               file_data.get(self._default_locale) or \
                               next(iter(file_data.values()), {})
                    if isinstance(file_obj, dict):
                        url = file_obj.get("url", "")
                        return f"https:{url}" if url.startswith("//") else url
        return ""

    def _get_entry(self, entry_id):
        if entry_id not in self._entry_cache:
            self._entry_cache[entry_id] = self.client.get_entry(entry_id)
        return self._entry_cache[entry_id]

    def _get_entries_batch(self, entry_ids):
        uncached = [eid for eid in entry_ids if eid not in self._entry_cache]
        if uncached:
            for i in range(0, len(uncached), 90):
                batch = uncached[i:i + 90]
                entries = self.client.get_entries_by_ids(batch)
                for e in entries:
                    self._entry_cache[e["sys"]["id"]] = e
        # Return in original order
        return [self._entry_cache[eid] for eid in entry_ids if eid in self._entry_cache]

    def _get_asset(self, asset_id):
        if asset_id not in self._asset_cache:
            try:
                self._asset_cache[asset_id] = self.client.get_asset(asset_id)
            except Exception:
                return {}
        return self._asset_cache[asset_id]


# ═══════════════════════════════════════════════════════════════════════
# Text Formatter
# ═══════════════════════════════════════════════════════════════════════

def format_as_text(course):
    lines = []
    lines.append("TUTORIAL EXTRACTION")
    lines.append("=" * 60)
    lines.append(f"Course: {course['name']}")
    lines.append(f"Course ID: {course['course_id']}")
    lines.append(f"Language: {course['locale']}")
    lines.append(f"Extracted: {course['extracted_at']}")
    lines.append(f"Stats: {course['stats']}")
    lines.append("")

    for i, lesson in enumerate(course["lessons"]):
        lines.append(f"\n{'─'*60}")
        lines.append(f"LESSON {i+1}: {lesson['name']}")
        lines.append(f"ID: {lesson['lesson_id']}")
        if lesson.get("objectives"):
            lines.append(f"Objectives: {'; '.join(lesson['objectives'])}")
        lines.append("")

        for activity in lesson["activities"]:
            lines.append(f"\n  ▸ Activity: {activity['name']}")

            for comp in activity["components"]:
                ctype = comp.get("type", "?")

                if ctype == "simpleText":
                    for p in comp.get("paragraphs", []):
                        lines.append(f"    {p}")
                    lines.append("")

                elif ctype == "list":
                    if comp.get("displayTitle"):
                        lines.append(f"    {comp['displayTitle']}")
                    for idx, item in enumerate(comp.get("items", []), 1):
                        bullet = f"    {idx}." if comp.get("listType") == "Numbered" else "    •"
                        lines.append(f"    {bullet} {item}")
                    lines.append("")

                elif ctype == "fact":
                    if comp.get("displayTitle"):
                        lines.append(f"    📌 {comp['displayTitle']}")
                    if comp.get("paragraph"):
                        lines.append(f"    {comp['paragraph']}")
                    lines.append("")

                elif ctype == "quote":
                    lines.append(f"    💬 \"{comp.get('displayQuote', '')}\"")
                    if comp.get("source"):
                        src = comp["source"]
                        lines.append(f"       — {src.get('name', '')} ({src.get('about', '')})")
                    lines.append("")

                elif ctype == "contentfulImage":
                    lines.append(f"    [IMAGE: {comp.get('imageUrl', '')}]")
                    lines.append("")

                elif ctype == "videoEmbedded":
                    title = comp.get("internalTitle", "Video")
                    dur = comp.get("videoDuration", "")
                    lines.append(f"    [VIDEO-EMBEDDED: {title} ({dur})]")
                    if comp.get("muxIdLightMode"):
                        lines.append(f"      Light: {comp['muxIdLightMode']}")
                    if comp.get("muxIdDarkMode"):
                        lines.append(f"      Dark:  {comp['muxIdDarkMode']}")
                    lines.append("")

                elif ctype == "video":
                    title = comp.get("internalTitle", "Video")
                    dur = comp.get("videoDuration", "")
                    lines.append(f"    [VIDEO: {title} ({dur})]")
                    if comp.get("muxId"):
                        lines.append(f"      Mux: {comp['muxId']}")
                    lines.append("")

                elif ctype == "image":
                    lines.append(f"    [IMAGE: {comp.get('internalName', '')}]")
                    if comp.get("lightModeUrl"):
                        lines.append(f"      Light: {comp['lightModeUrl']}")
                    if comp.get("darkModeUrl"):
                        lines.append(f"      Dark:  {comp['darkModeUrl']}")
                    lines.append("")

                elif ctype == "textAndLink":
                    for p in comp.get("paragraphs", []):
                        lines.append(f"    {p}")
                    if comp.get("link"):
                        link = comp["link"]
                        lines.append(f"    🔗 {link.get('displayText', '')} → {link.get('destinationUrl', '')}")
                    lines.append("")

                elif ctype == "multiChoiceQuestion":
                    lines.append(f"    ❓ {comp.get('question', '')}")
                    for idx, ans in enumerate(comp.get("answers", []), 1):
                        correct = " ✅" if comp.get("correctAnswer") == idx else ""
                        lines.append(f"      {idx}. {ans['text']}{correct}")
                        if ans.get("info"):
                            lines.append(f"         ℹ️  {ans['info']}")
                    lines.append("")

                else:
                    lines.append(f"    [{ctype}: {json.dumps(comp.get('fields', comp), ensure_ascii=False)[:100]}]")
                    lines.append("")

    # Quizzes
    if course.get("quizzes"):
        lines.append(f"\n{'═'*60}")
        lines.append("QUIZZES")
        lines.append(f"{'═'*60}")
        for quiz in course["quizzes"]:
            lines.append(f"\n📝 {quiz['name']} (min score: {quiz.get('min_score', 'N/A')})")
            for q in quiz.get("questions", []):
                if q.get("type") == "multiChoiceQuestion":
                    lines.append(f"\n  ❓ {q.get('question', '')}")
                    for idx, ans in enumerate(q.get("answers", []), 1):
                        correct = " ✅" if q.get("correctAnswer") == idx else ""
                        lines.append(f"    {idx}. {ans['text']}{correct}")
                        if ans.get("info"):
                            lines.append(f"       ℹ️  {ans['info']}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Extract Lyft tutorial content from Contentful")
    parser.add_argument("--course", action="append", required=True, help="Course or container ID")
    parser.add_argument("--name", default=None, help="Custom output name")
    parser.add_argument("--languages", default=None, help="Comma-separated locales (default: all)")
    parser.add_argument("--output", default="./output", help="Output directory")
    parser.add_argument("--json-only", action="store_true", help="Skip text file output")
    parser.add_argument("--list-courses", action="store_true", help="List all courseContainer entries")
    args = parser.parse_args()

    space_id = os.environ.get("CONTENTFUL_SPACE_ID")
    cma_token = os.environ.get("CONTENTFUL_CMA_TOKEN")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    if not space_id or not cma_token:
        print("❌ Set CONTENTFUL_SPACE_ID and CONTENTFUL_CMA_TOKEN in .env")
        sys.exit(1)

    client = ContentfulClient(space_id, cma_token, env_id)
    extractor = CourseExtractor(client)
    extractor.discover_locales()

    if args.list_courses:
        print("\n📚 All courseContainer entries:")
        containers = client.get_entries_by_type("courseContainer")
        for c in containers:
            cid = c["sys"]["id"]
            fields = c.get("fields", {})
            name = fields.get("internalName", {})
            first_name = next(iter(name.values()), "?") if isinstance(name, dict) else name
            print(f"  {cid}: {first_name}")
        return

    locales = None
    if args.languages:
        locales = [l.strip() for l in args.languages.split(",")]

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    for course_id in args.course:
        print(f"\n{'='*60}")
        print(f"Extracting: {course_id}")
        print(f"{'='*60}")

        courses = extractor.extract_all_locales(course_id, locales)
        if not courses:
            print(f"❌ No data for {course_id}")
            continue

        first_locale = next(iter(courses))
        course_name = args.name or courses[first_locale]["name"] or course_id
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in course_name).strip()

        course_dir = out_dir / safe_name
        course_dir.mkdir(parents=True, exist_ok=True)

        # All languages combined
        with open(course_dir / f"{safe_name}_all_languages.json", "w", encoding="utf-8") as f:
            json.dump(courses, f, indent=2, ensure_ascii=False)

        # Per-language files
        for locale, data in courses.items():
            short = locale.split("-")[0] if "-" in locale else locale
            suffix = "" if locale == extractor._default_locale else f"_{short}"

            with open(course_dir / f"{safe_name}{suffix}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            if not args.json_only:
                with open(course_dir / f"{safe_name}{suffix}.txt", "w", encoding="utf-8") as f:
                    f.write(format_as_text(data))

            print(f"  💾 {locale}: {safe_name}{suffix}.json" +
                  (f" + .txt" if not args.json_only else ""))

        print(f"\n✅ Saved to {course_dir.resolve()}")

    print(f"\n🎉 Done!")


if __name__ == "__main__":
    main()