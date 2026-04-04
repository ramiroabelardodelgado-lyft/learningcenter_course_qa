# LyftLearn Course QA Pipeline

Automated language QA for LyftLearn course content across English, Spanish, French, and Portuguese.

## Repo Structure

```
lyftlearn-qa/                        ← clone to $HOME/studio
├── bootstrap.sh                     ← one-command setup for fresh instances
├── activate.sh                      ← (generated) source before manual runs
├── .env                             ← (pulled from S3) secrets — not in git
├── .env.example                     ← template
├── .gitignore
├── extract_course.py                ← Contentful CMA walker
├── language_qa.py                   ← LLM QA engine (2-pass verification)
├── persistent-packages/             ← (generated) pip packages — not in git
├── output/                          ← (generated) results per course — not in git
│   └── {course_name}/
│       ├── *_all_languages.json
│       ├── *_es.json / *_es.txt
│       ├── {job_id}_qa_full.csv
│       └── {job_id}_qa_issues.csv
└── slack_bot/
    ├── __init__.py
    ├── runner.py                    ← pipeline orchestrator
    ├── poller.py                    ← S3 job queue (continuous)
    └── github_bridge.py            ← GitHub ↔ S3 translator
```

Everything lives inside this one directory. No ~/.bashrc changes, no files
scattered across $HOME.

## Weekly Workflow

```
1. go/ml → Start instance (~2 min)
2. llt ssh connect
3. git clone → bootstrap.sh → ready (~3 min)
4. Run QA (or start poller + bridge for Slack jobs)
5. go/ml → Stop instance
```

## Setup (fresh instance)

```bash
cd $HOME
git clone git@github.com:ramiroabelardodelgado-lyft/lyftlearn-qa.git studio
cd studio
bash bootstrap.sh
```

Bootstrap does everything: installs packages, pulls .env from S3, verifies setup.

## Running Manually

```bash
cd $HOME/studio
source activate.sh   # sets PYTHONPATH for this shell session

# Extract a course
python extract_course.py --course 2yQq04tUUk1H67xlZA7PLn --name "De-escalation"

# Run QA (skip English — it's the source language)
python language_qa.py --input ./output/De-escalation/ --skip-en --csv --save
```

## Running via Slack (background services)

These scripts patch their own sys.path — no `source activate.sh` needed:

```bash
cd $HOME/studio
nohup python slack_bot/poller.py > poller.log 2>&1 &
nohup python slack_bot/github_bridge.py > github_bridge.log 2>&1 &
```

## First-Time: Store .env in S3

Before your first instance is deleted, back up secrets to S3:

```bash
aws s3 cp .env s3://lyft-lyftlearn-production-iad/course-qa/config/.env
```

Future instances pull it automatically via `bootstrap.sh`.

## Known Gotchas

| # | Issue | Fix |
|---|-------|-----|
| 7 | AWS_PROFILE breaks container auth | bootstrap.sh strips it automatically |
| 8 | ~ doesn't expand in double quotes | Use $HOME in bash, Path.home() in Python |
| 9 | PYTHONPATH not set in non-interactive SSH | runner/poller patch sys.path themselves |
| 10 | Course ID typos | Case-sensitive. 2yQq04tUUk1H67xlZA7PLn (double-U) |

Full list: see roadblocks.md in project docs.
