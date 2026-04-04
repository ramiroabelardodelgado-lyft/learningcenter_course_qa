#!/usr/bin/env bash
# bootstrap.sh — Run once after cloning on a fresh instance
# Usage: cd $HOME/studio && bash bootstrap.sh
#
# Everything stays inside this directory — no ~/.bashrc changes,
# no files scattered across $HOME.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGES_DIR="$SCRIPT_DIR/persistent-packages"
S3_BUCKET="lyft-lyftlearn-production-iad"
S3_ENV="s3://$S3_BUCKET/course-qa/config/.env"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     LyftLearn QA Pipeline — Bootstrap        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Create directories ────────────────────────────────────────
echo "📁 Creating directories..."
mkdir -p "$PACKAGES_DIR"
mkdir -p "$SCRIPT_DIR/output"

# ── Step 2: Install Python dependencies ───────────────────────────────
echo ""
echo "📦 Installing Python packages to ./persistent-packages/"
echo "   (This takes 2-3 minutes on a fresh instance)"
echo ""

# Lyft-internal packages (from Artifactory)
pip install lyft-llm langchain-core langchain-aws langchain-openai \
  --index-url https://artifactory.lyft.net/artifactory/api/pypi/virtual-pypi-lyft-jammy/simple/ \
  --target "$PACKAGES_DIR" \
  --quiet 2>&1 | tail -5

# Standard packages (from public PyPI — Artifactory may miss ARM64 wheels)
pip install requests python-dotenv boto3 \
  --target "$PACKAGES_DIR" \
  --index-url https://pypi.org/simple/ \
  --quiet 2>&1 | tail -5

echo "   ✅ Packages installed"

# ── Step 3: Generate activate.sh ──────────────────────────────────────
# Instead of modifying ~/.bashrc, generate a local activate script.
# Source it before running commands manually:  source activate.sh
#
# Not needed for poller/runner/bridge — they patch sys.path themselves.
# Only needed for running extract_course.py or language_qa.py directly.
echo ""
echo "🔧 Generating activate.sh..."
cat > "$SCRIPT_DIR/activate.sh" << 'ACTIVATE_EOF'
#!/usr/bin/env bash
# Source this before running scripts manually:
#   source activate.sh
#
# Not needed for poller/runner/bridge — they patch sys.path themselves.
# Only needed for running extract_course.py or language_qa.py directly.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR/persistent-packages:$SCRIPT_DIR:$PYTHONPATH"
echo "✅ PYTHONPATH set — ready to run scripts"
ACTIVATE_EOF
echo "   Created activate.sh"

# ── Step 4: Pull .env from S3 ────────────────────────────────────────
echo ""
echo "🔐 Pulling .env from S3..."
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "   .env already exists — skipping (delete it to re-pull from S3)"
else
    if aws s3 cp "$S3_ENV" "$SCRIPT_DIR/.env" 2>/dev/null; then
        echo "   ✅ .env pulled from $S3_ENV"
    else
        echo "   ⚠️  Could not pull .env from S3"
        echo "   Create it manually: cp .env.example .env && nano .env"
    fi
fi

# Safety check: .env must NOT contain AWS_PROFILE (Roadblock #7)
if [ -f "$SCRIPT_DIR/.env" ] && grep -q "AWS_PROFILE" "$SCRIPT_DIR/.env"; then
    echo "   ⚠️  Removing AWS_PROFILE from .env (breaks container auth)"
    grep -v "AWS_PROFILE" "$SCRIPT_DIR/.env" > /tmp/.env.clean
    mv /tmp/.env.clean "$SCRIPT_DIR/.env"
fi

# ── Step 5: Verify ────────────────────────────────────────────────────
echo ""
echo "🔍 Verifying setup..."

export PYTHONPATH="$PACKAGES_DIR:$SCRIPT_DIR:$PYTHONPATH"

PASS=true

python -c "import lyft_llm" 2>/dev/null && echo "   ✅ lyft_llm" || { echo "   ❌ lyft_llm not found"; PASS=false; }
python -c "import boto3" 2>/dev/null && echo "   ✅ boto3" || { echo "   ❌ boto3 not found"; PASS=false; }
python -c "import requests" 2>/dev/null && echo "   ✅ requests" || { echo "   ❌ requests not found"; PASS=false; }

[ -f "$SCRIPT_DIR/extract_course.py" ] && echo "   ✅ extract_course.py" || { echo "   ❌ extract_course.py missing"; PASS=false; }
[ -f "$SCRIPT_DIR/language_qa.py" ] && echo "   ✅ language_qa.py" || { echo "   ❌ language_qa.py missing"; PASS=false; }
[ -f "$SCRIPT_DIR/slack_bot/runner.py" ] && echo "   ✅ slack_bot/runner.py" || { echo "   ❌ slack_bot/runner.py missing"; PASS=false; }
[ -f "$SCRIPT_DIR/slack_bot/poller.py" ] && echo "   ✅ slack_bot/poller.py" || { echo "   ❌ slack_bot/poller.py missing"; PASS=false; }

if [ -f "$SCRIPT_DIR/.env" ]; then
    grep -q "CONTENTFUL_SPACE_ID" "$SCRIPT_DIR/.env" && echo "   ✅ .env has CONTENTFUL_SPACE_ID" || echo "   ⚠️  .env missing CONTENTFUL_SPACE_ID"
    grep -q "CONTENTFUL_CMA_TOKEN" "$SCRIPT_DIR/.env" && echo "   ✅ .env has CONTENTFUL_CMA_TOKEN" || echo "   ⚠️  .env missing CONTENTFUL_CMA_TOKEN"
else
    echo "   ⚠️  No .env file — create from .env.example"
    PASS=false
fi

echo ""
if [ "$PASS" = true ]; then
    echo "✅ Bootstrap complete! Ready to run."
    echo ""
    echo "   For manual script runs, first:  source activate.sh"
    echo ""
    echo "   Quick test (dry run):  "
    echo "   python extract_course.py --course 2yQq04tUUk1H67xlZA7PLn --name 'De-escalation'"
    echo "   python language_qa.py --input ./output/De-escalation/ --skip-en --csv --dry-run"
    echo ""
    echo "   Start background services (no activate.sh needed):  "
    echo "   nohup python slack_bot/poller.py > poller.log 2>&1 &"
    echo "   nohup python slack_bot/github_bridge.py > github_bridge.log 2>&1 &"
else
    echo "⚠️  Some checks failed — see above"
fi
echo ""
