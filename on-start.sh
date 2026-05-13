#!/bin/bash
set -e

pip install lyft-llm --extra-index-url https://pypi.lyft.net/pypi --quiet
pip install langchain-core langchain-aws langchain-openai --quiet