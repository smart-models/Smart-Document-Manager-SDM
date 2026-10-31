#!/usr/bin/env bash
# Replace {{REPO}} and {{INPUT_ROOT}} before running.
set -eu
cp "{{REPO}}/calibration/multipage.pdf" "{{INPUT_ROOT}}/_eval_multipage.pdf"
