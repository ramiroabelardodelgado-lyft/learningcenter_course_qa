#!/usr/bin/env bash
# bootstrap.sh — Run once after cloning on a fresh LyftLearn Agent instance
# Usage: cd $HOME/studio && bash bootstrap.sh
#
# LyftLearn Agent image (Python 3.13) has all dependencies pre-installed.
# This script only: pulls .env from S3, verifies imports, runs LLM smoke test.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
S3_BUCKET="lyft-lyftlearn-production-iad"
S3_ENV="s3://$S3_BUCKET/course-qa/config/.env"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  LyftLearn QA Pipeline — Bootstrap           ║"
echo "║  Image: LyftLearn Agent (Python 3.13)        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Directories ───────────────────────────────────────────────
echo "📁 Creating directories..."
mkdir -p "$SCRIPT_DIR/output"

# ── Step 2: Pull .env from S3 ────────────────────────────────────────
echo ""
echo "🔐 Pulling .env from S3..."
if [ -f "$SCRIPT_DIR/.env" ]; then
  echo "  .env already exists — skipping (delete to re-pull)"
else
  if aws s3 cp "$S3_ENV" "$SCRIPT_DIR/.env" 2>/dev/null; then
    echo "  ✅ .env pulled from S3"
  else
    echo "  ⚠️  Could not pull .env — create manually from env_template.txt"
  fi
fi

# Strip AWS_PROFILE — breaks container credentials (Roadblock #7)
if [ -f "$SCRIPT_DIR/.env" ] && grep -q "AWS_PROFILE" "$SCRIPT_DIR/.env"; then
  echo "  ⚠️  Removing AWS_PROFILE from .env (breaks container auth)"
  grep -v "AWS_PROFILE" "$SCRIPT_DIR/.env" > /tmp/.env.clean
  mv /tmp/.env.clean "$SCRIPT_DIR/.env"
fi

# ── Step 3: Verify system dependencies ───────────────────────────────
echo ""
echo "🔍 Verifying system dependencies (Agent image — no install needed)..."
PASS=true

python3 -c "import lyft_llm" 2>/dev/null \
  && echo "  ✅ lyft_llm" \
  || { echo "  ❌ lyft_llm — are you on the LyftLearn Agent image?"; PASS=false; }

python3 -c "import langchain_aws" 2>/dev/null \
  && echo "  ✅ langchain_aws" \
  || { echo "  ❌ langchain_aws"; PASS=false; }

python3 -c "import boto3" 2>/dev/null \
  && echo "  ✅ boto3" \
  || { echo "  ❌ boto3"; PASS=false; }

python3 -c "import requests" 2>/dev/null \
  && echo "  ✅ requests" \
  || { echo "  ❌ requests"; PASS=false; }

# ── Step 4: Verify scripts ───────────────────────────────────────────
echo ""
echo "🔍 Verifying scripts..."
[ -f "$SCRIPT_DIR/extract_course.py" ]          && echo "  ✅ extract_course.py"          || { echo "  ❌ extract_course.py missing";          PASS=false; }
[ -f "$SCRIPT_DIR/language_qa.py" ]             && echo "  ✅ language_qa.py"             || { echo "  ❌ language_qa.py missing";             PASS=false; }
[ -f "$SCRIPT_DIR/slack_bot/runner.py" ]        && echo "  ✅ slack_bot/runner.py"        || { echo "  ❌ slack_bot/runner.py missing";        PASS=false; }
[ -f "$SCRIPT_DIR/slack_bot/poller.py" ]        && echo "  ✅ slack_bot/poller.py"        || { echo "  ❌ slack_bot/poller.py missing";        PASS=false; }
[ -f "$SCRIPT_DIR/slack_bot/github_bridge.py" ] && echo "  ✅ slack_bot/github_bridge.py" || { echo "  ❌ slack_bot/github_bridge.py missing"; PASS=false; }

# ── Step 5: Verify .env ──────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
  grep -q "CONTENTFUL_SPACE_ID"  "$SCRIPT_DIR/.env" && echo "  ✅ .env: CONTENTFUL_SPACE_ID"  || echo "  ⚠️  .env missing CONTENTFUL_SPACE_ID"
  grep -q "CONTENTFUL_CMA_TOKEN" "$SCRIPT_DIR/.env" && echo "  ✅ .env: CONTENTFUL_CMA_TOKEN" || echo "  ⚠️  .env missing CONTENTFUL_CMA_TOKEN"
else
  echo "  ⚠️  No .env — create from env_template.txt"; PASS=false
fi

# ── Step 6: LLM smoke test ───────────────────────────────────────────
echo ""
echo "🤖 Testing LLM connection (claude-sonnet-4-6)..."
python3 -c "
import lyft_llm.integrations.langchain as llc
chat = llc.make_llm(model_id='us.anthropic.claude-sonnet-4-6', model_kwargs={'temperature': 0})
resp = chat.invoke('say ok in one word')
print('  ✅ LLM works:', resp.content)
" 2>/dev/null || echo "  ⚠️  LLM test failed — check AWS credentials"

# ── Done ─────────────────────────────────────────────────────────────
echo ""
if [ "$PASS" = true ]; then
  echo "✅ Bootstrap complete! Ready to run."
  echo ""
  echo "  Full run:"
  echo "    python3 extract_course.py --course 2yQq04tUUk1H67xlZA7PLn --name 'De-escalation'"
  echo "    python3 language_qa.py --input ./output/De-escalation/ --skip-en --csv --save"
  echo ""
  echo "  Background services:"
  echo "    nohup python3 slack_bot/poller.py > poller.log 2>&1 &"
  echo "    nohup python3 slack_bot/github_bridge.py > github_bridge.log 2>&1 &"
else
  echo "⚠️  Some checks failed. Are you on the LyftLearn Agent image?"
fi
echo ""
