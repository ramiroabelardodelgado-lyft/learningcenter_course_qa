#!/usr/bin/env python3
"""
Content Model Discovery Script (Self-Contained)
=================================================
Single file, no local imports. Just needs: pip install requests python-dotenv

Walks the Contentful reference tree from course containers and maps
the actual content type hierarchy + field names at each level.

Usage:
    python discover_model.py
    python discover_model.py --course 2yQq04tUUk1H67xlZA7PLn
    python discover_model.py --course ID1 --course ID2 --depth 8

Output:
    output/content_model_map.json
    output/content_type_summary.json
    output/hierarchy_chains.json
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed, reading env vars directly")

import requests


# ── Inline Contentful Client ──────────────────────────────────────────

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
            params={"sys.id[in]": ",".join(entry_ids), "limit": len(entry_ids)},
        )
        return resp.get("items", [])

    def get_asset(self, asset_id):
        return self._get(f"{self._base}/assets/{asset_id}")

    def _get(self, url, params=None, retries=3):
        for attempt in range(retries):
            resp = self.session.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = int(resp.headers.get("X-Contentful-RateLimit-Reset", 2))
                print(f"  ⏳ Rate limited. Waiting {wait}s...")
                time.sleep(wait)
            elif resp.status_code == 404:
                return {}
            else:
                print(f"  ⚠️ HTTP {resp.status_code} for {url}")
                return {}
        return {}


# ── Discovery Engine ──────────────────────────────────────────────────

class ModelDiscovery:

    def __init__(self, client):
        self.client = client
        self._entry_cache = {}
        self._asset_cache = {}
        self.content_types = {}
        self.hierarchy_paths = []
        self.trees = []

    def walk_entry(self, entry_id, max_depth=6):
        return self._walk(entry_id, depth=0, max_depth=max_depth, path=[])

    def _walk(self, entry_id, depth, max_depth, path):
        if depth > max_depth:
            return {"_truncated": True, "_depth": depth}

        entry = self._get_entry(entry_id)
        if not entry or "sys" not in entry:
            return {"_error": f"Entry {entry_id} not found"}

        ct_id = entry.get("sys", {}).get("contentType", {}).get("sys", {}).get("id", "unknown")
        fields = entry.get("fields", {})

        self._record_content_type(ct_id, fields)
        current_path = path + [ct_id]

        node = {
            "_id": entry_id,
            "_content_type": ct_id,
            "_depth": depth,
            "_fields_summary": {},
            "_children": [],
        }

        for field_name, field_value in fields.items():
            if not isinstance(field_value, dict):
                node["_fields_summary"][field_name] = str(type(field_value).__name__)
                continue

            first_key = next(iter(field_value), None)
            if not first_key:
                continue
            first_val = field_value[first_key]
            locales_present = list(field_value.keys())

            if isinstance(first_val, str):
                preview = first_val[:60].replace("\n", " ")
                node["_fields_summary"][field_name] = {
                    "type": "String",
                    "preview": preview,
                    "locales": locales_present,
                }

            elif isinstance(first_val, (int, float, bool)):
                node["_fields_summary"][field_name] = {
                    "type": type(first_val).__name__,
                    "value": first_val,
                }

            elif isinstance(first_val, dict):
                if "sys" in first_val:
                    link_type = first_val["sys"].get("linkType", first_val["sys"].get("type", "?"))
                    link_id = first_val["sys"].get("id", "?")

                    node["_fields_summary"][field_name] = {
                        "type": f"Link<{link_type}>",
                        "target_id": link_id,
                    }

                    if link_type == "Entry":
                        child = self._walk(link_id, depth + 1, max_depth, current_path)
                        child["_via_field"] = field_name
                        node["_children"].append(child)
                    elif link_type == "Asset":
                        node["_fields_summary"][field_name]["type"] = "Asset"
                        asset = self._get_asset(link_id)
                        if asset:
                            file_field = asset.get("fields", {}).get("file", {})
                            first_file = next(iter(file_field.values()), {}) if isinstance(file_field, dict) else {}
                            ct = first_file.get("contentType", "?") if isinstance(first_file, dict) else "?"
                            node["_fields_summary"][field_name]["asset_content_type"] = ct

                elif "nodeType" in first_val:
                    node["_fields_summary"][field_name] = {
                        "type": "RichText",
                        "node_type": first_val.get("nodeType"),
                    }
                else:
                    node["_fields_summary"][field_name] = {
                        "type": "Object",
                        "keys": list(first_val.keys())[:10],
                    }

            elif isinstance(first_val, list):
                if len(first_val) == 0:
                    node["_fields_summary"][field_name] = {"type": "Array (empty)"}

                elif isinstance(first_val[0], dict) and "sys" in first_val[0]:
                    link_type = first_val[0]["sys"].get("linkType", first_val[0]["sys"].get("type", "?"))
                    ref_ids = [item["sys"]["id"] for item in first_val if "sys" in item]

                    node["_fields_summary"][field_name] = {
                        "type": f"Array<Link<{link_type}>>",
                        "count": len(first_val),
                        "locales": locales_present,
                    }

                    if link_type == "Entry":
                        sample_ids = ref_ids[:3]
                        sample_entries = self._get_entries_batch(sample_ids)
                        for child_entry in sample_entries:
                            child_id = child_entry["sys"]["id"]
                            child = self._walk(child_id, depth + 1, max_depth, current_path)
                            child["_via_field"] = field_name
                            child["_sample_of"] = len(ref_ids)
                            node["_children"].append(child)

                elif isinstance(first_val[0], str):
                    node["_fields_summary"][field_name] = {
                        "type": "Array<String>",
                        "count": len(first_val),
                        "sample": first_val[:3],
                    }
                else:
                    node["_fields_summary"][field_name] = {
                        "type": f"Array<{type(first_val[0]).__name__}>",
                        "count": len(first_val),
                    }

        if not node["_children"]:
            self.hierarchy_paths.append(current_path)

        return node

    def _record_content_type(self, ct_id, fields):
        if ct_id not in self.content_types:
            self.content_types[ct_id] = {
                "content_type_id": ct_id,
                "fields": {},
                "seen_count": 0,
            }
        self.content_types[ct_id]["seen_count"] += 1

        for field_name, field_value in fields.items():
            if field_name in self.content_types[ct_id]["fields"]:
                continue
            if not isinstance(field_value, dict):
                self.content_types[ct_id]["fields"][field_name] = "unknown"
                continue
            first_val = next(iter(field_value.values()), None)
            if isinstance(first_val, str):
                self.content_types[ct_id]["fields"][field_name] = "String"
            elif isinstance(first_val, bool):
                self.content_types[ct_id]["fields"][field_name] = "Boolean"
            elif isinstance(first_val, (int, float)):
                self.content_types[ct_id]["fields"][field_name] = "Number"
            elif isinstance(first_val, list):
                if first_val and isinstance(first_val[0], dict) and "sys" in first_val[0]:
                    lt = first_val[0]["sys"].get("linkType", "?")
                    self.content_types[ct_id]["fields"][field_name] = f"Array<Link<{lt}>>"
                elif first_val and isinstance(first_val[0], str):
                    self.content_types[ct_id]["fields"][field_name] = "Array<String>"
                else:
                    self.content_types[ct_id]["fields"][field_name] = "Array"
            elif isinstance(first_val, dict):
                if "sys" in first_val:
                    lt = first_val["sys"].get("linkType", "?")
                    self.content_types[ct_id]["fields"][field_name] = f"Link<{lt}>"
                elif "nodeType" in first_val:
                    self.content_types[ct_id]["fields"][field_name] = "RichText"
                else:
                    self.content_types[ct_id]["fields"][field_name] = "Object"
            else:
                self.content_types[ct_id]["fields"][field_name] = type(first_val).__name__ if first_val else "null"

    # ── Pretty Print ──────────────────────────────────────────────────

    def print_tree(self, node, indent=0):
        prefix = "  " * indent
        connector = "└── " if indent > 0 else ""
        pad = "    " if indent > 0 else ""
        ct = node.get("_content_type", "?")
        entry_id = node.get("_id", "?")
        via = f" (via: {node['_via_field']})" if "_via_field" in node else ""
        sample = f" [sampled 3 of {node['_sample_of']}]" if "_sample_of" in node else ""

        if node.get("_truncated"):
            print(f"{prefix}{connector}... truncated at depth {node['_depth']}")
            return
        if node.get("_error"):
            print(f"{prefix}{connector}WARNING: {node['_error']}")
            return

        print(f"{prefix}{connector}[{ct}]{via}{sample}")
        print(f"{prefix}{pad}   id: {entry_id}")

        for fname, finfo in node.get("_fields_summary", {}).items():
            if isinstance(finfo, dict):
                ftype = finfo.get("type", "?")
                extra = ""
                if "preview" in finfo:
                    extra = f' = "{finfo["preview"]}"'
                elif "count" in finfo:
                    extra = f" ({finfo['count']} items)"
                elif "value" in finfo:
                    extra = f" = {finfo['value']}"
                elif "asset_content_type" in finfo:
                    extra = f" [{finfo['asset_content_type']}]"
                print(f"{prefix}{pad}   - {fname}: {ftype}{extra}")
            else:
                print(f"{prefix}{pad}   - {fname}: {finfo}")

        for child in node.get("_children", []):
            self.print_tree(child, indent + 1)

    def print_summary(self):
        print(f"\n{'='*70}")
        print("CONTENT TYPE SUMMARY")
        print(f"{'='*70}")

        for ct_id, info in sorted(self.content_types.items()):
            print(f"\n  {ct_id} (seen {info['seen_count']}x)")
            for fname, ftype in info["fields"].items():
                print(f"     - {fname}: {ftype}")

        unique_paths = sorted(set(" > ".join(p) for p in self.hierarchy_paths))
        print(f"\n{'='*70}")
        print(f"OBSERVED HIERARCHY CHAINS ({len(unique_paths)} unique)")
        print(f"{'='*70}")
        for p in unique_paths:
            print(f"  {p}")

    # ── Cache helpers ─────────────────────────────────────────────────

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
                for entry in entries:
                    self._entry_cache[entry["sys"]["id"]] = entry
        return [self._entry_cache[eid] for eid in entry_ids if eid in self._entry_cache]

    def _get_asset(self, asset_id):
        if asset_id not in self._asset_cache:
            try:
                self._asset_cache[asset_id] = self.client.get_asset(asset_id)
            except Exception:
                return {}
        return self._asset_cache[asset_id]


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Discover Contentful content model from real entries")
    parser.add_argument("--course", action="append", help="Course container ID(s)")
    parser.add_argument("--depth", type=int, default=6, help="Max depth (default: 6)")
    parser.add_argument("--output", default="./output", help="Output dir")
    args = parser.parse_args()

    space_id = os.environ.get("CONTENTFUL_SPACE_ID")
    cma_token = os.environ.get("CONTENTFUL_CMA_TOKEN")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    if not space_id or not cma_token:
        print("Missing credentials. Set them via .env or environment variables:")
        print("  CONTENTFUL_SPACE_ID=kdr36sxfa9m3")
        print("  CONTENTFUL_CMA_TOKEN=CFPAT-...")
        sys.exit(1)

    client = ContentfulClient(space_id, cma_token, env_id)
    discovery = ModelDiscovery(client)

    course_ids = args.course or ["2yQq04tUUk1H67xlZA7PLn"]

    print(f"Walking {len(course_ids)} course(s), max depth {args.depth}\n")

    for course_id in course_ids:
        print(f"\n{'='*70}")
        print(f"COURSE: {course_id}")
        print(f"{'='*70}\n")

        tree = discovery.walk_entry(course_id, max_depth=args.depth)
        discovery.trees.append(tree)
        discovery.print_tree(tree)

    discovery.print_summary()

    # Save
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "content_model_map.json", "w") as f:
        json.dump(discovery.trees, f, indent=2, ensure_ascii=False)

    with open(out_dir / "content_type_summary.json", "w") as f:
        json.dump(discovery.content_types, f, indent=2, ensure_ascii=False)

    chains = sorted(set(" > ".join(p) for p in discovery.hierarchy_paths))
    with open(out_dir / "hierarchy_chains.json", "w") as f:
        json.dump(chains, f, indent=2)

    print(f"\nSaved to {out_dir.resolve()}/")
    print(f"  - content_model_map.json   (full tree)")
    print(f"  - content_type_summary.json (all content types + fields)")
    print(f"  - hierarchy_chains.json     (nesting paths)")
    print(f"\nShare those files back and we'll map the extractor to the real model.")


if __name__ == "__main__":
    main()
