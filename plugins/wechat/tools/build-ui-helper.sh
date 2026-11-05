#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The existing helper's XML dump is package-neutral; only the WeChat parser filters packages.
bash "$project_root/../xiaohongshu/tools/build-ui-helper.sh" \
  "$project_root/wechat_channels/collector/ui-dump.dex"
