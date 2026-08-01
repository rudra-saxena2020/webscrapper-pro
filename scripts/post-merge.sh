#!/bin/bash
set -e

npm install --prefer-offline 2>&1 || npm install

pip install -r requirements.txt -q 2>&1 || true
