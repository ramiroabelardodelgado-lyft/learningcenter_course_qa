#!/usr/bin/env python3
"""
List ALL content types in the Contentful space with full field details.
No sampling needed — this queries the schema directly.

Usage:
    python list_content_types.py
    python list_content_types.py --filter component
    python list_content_types.py --output ./my_output

Dependencies:
    pip install requests python-dotenv
"""

import os
import sys
import json
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests


def main():
    parser = argparse.ArgumentParser(description="List all Contentful content types")
    parser.add_argument("--filter", "-f", default=None, help="Filter by keyword (e.g. 'component')")
    parser.add_argument("--output", default="./output", help="Output directory")
    args = parser.parse_args()

    space_id = os.environ.get("CONTENTFUL_SPACE_ID")
    cma_token = os.environ.get("CONTENTFUL_CMA_TOKEN")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    if not space_id or not cma_token:
        print("❌ Set CONTENTFUL_SPACE_ID and CONTENTFUL_CMA_TOKEN in .env")
        sys.exit(1)

    url = f"https://api.contentful.com/spaces/{space_id}/environments/{env_id}/content_types"
    headers = {"Authorization": f"Bearer {cma_token}"}

    # Paginate through all content types
    all_types = []
    skip = 0
    limit = 100
    while True:
        resp = requests.get(url, headers=headers, params={"limit": limit, "skip": skip})
        if resp.status_code != 200:
            print(f"❌ HTTP {resp.status_code}: {resp.text[:200]}")
            sys.exit(1)
        data = resp.json()
        items = data.get("items", [])
        all_types.extend(items)
        total = data.get("total", 0)
        skip += limit
        if skip >= total:
            break

    print(f"Found {len(all_types)} content types in space {space_id}/{env_id}\n")

    # Organize and display
    results = {}
    for ct in sorted(all_types, key=lambda x: x.get("name", "")):
        ct_id = ct["sys"]["id"]
        ct_name = ct.get("name", ct_id)
        ct_desc = ct.get("description", "")

        if args.filter and args.filter.lower() not in f"{ct_id} {ct_name} {ct_desc}".lower():
            continue

        fields_info = []
        for field in ct.get("fields", []):
            fid = field["id"]
            ftype = field["type"]
            localized = field.get("localized", False)
            required = field.get("required", False)
            disabled = field.get("disabled", False)

            # Resolve link/array details
            detail = ftype
            if ftype == "Link":
                detail = f"Link<{field.get('linkType', '?')}>"
                validations = field.get("validations", [])
                for v in validations:
                    if "linkContentType" in v:
                        detail += f" [{', '.join(v['linkContentType'])}]"
            elif ftype == "Array":
                items = field.get("items", {})
                itype = items.get("type", "?")
                if itype == "Link":
                    detail = f"Array<Link<{items.get('linkType', '?')}>>"
                    for v in items.get("validations", []):
                        if "linkContentType" in v:
                            detail += f" [{', '.join(v['linkContentType'])}]"
                elif itype == "Symbol":
                    detail = "Array<String>"
                else:
                    detail = f"Array<{itype}>"
            elif ftype == "RichText":
                detail = "RichText"
            elif ftype == "Symbol":
                detail = "String"
                validations = field.get("validations", [])
                for v in validations:
                    if "in" in v:
                        detail += f" (enum: {v['in']})"
            elif ftype == "Text":
                detail = "Text (long)"
            elif ftype == "Integer":
                detail = "Integer"
            elif ftype == "Number":
                detail = "Number"
            elif ftype == "Boolean":
                detail = "Boolean"

            flags = []
            if localized: flags.append("localized")
            if required: flags.append("required")
            if disabled: flags.append("disabled")
            flags_str = f" [{', '.join(flags)}]" if flags else ""

            fields_info.append({
                "id": fid,
                "type": detail,
                "localized": localized,
                "required": required,
                "disabled": disabled,
            })

            print(f"   {'🌐' if localized else '  '} {fid:35s} {detail}{flags_str}")

        results[ct_id] = {
            "id": ct_id,
            "name": ct_name,
            "description": ct_desc,
            "field_count": len(fields_info),
            "fields": fields_info,
        }

        # Print header
        desc = f" — {ct_desc}" if ct_desc else ""
        print(f"\n{'─'*60}")
        print(f"📦 {ct_name} (id: {ct_id}){desc}")
        print(f"   {len(fields_info)} fields:")
        for fi in fields_info:
            flags = []
            if fi["localized"]: flags.append("localized")
            if fi["required"]: flags.append("required")
            if fi["disabled"]: flags.append("disabled")
            flags_str = f" [{', '.join(flags)}]" if flags else ""
            loc_icon = "🌐" if fi["localized"] else "  "
            print(f"   {loc_icon} {fi['id']:35s} {fi['type']}{flags_str}")

    # Summary table
    print(f"\n{'═'*60}")
    print(f"SUMMARY: {len(results)} content types" +
          (f" (filtered by '{args.filter}')" if args.filter else ""))
    print(f"{'═'*60}")
    for ct_id, info in sorted(results.items()):
        loc_count = sum(1 for f in info["fields"] if f["localized"])
        print(f"  {ct_id:40s} {info['field_count']:2d} fields ({loc_count} localized)")

    # Save JSON
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "all_content_types.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Saved: {out_file}")


if __name__ == "__main__":
    main()
