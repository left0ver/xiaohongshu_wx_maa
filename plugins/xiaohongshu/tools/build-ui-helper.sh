#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="${ANDROID_HOME:?Set ANDROID_HOME}"
java_root="${JAVA_HOME:?Set JAVA_HOME to JDK 17}"
task_build_dir="$(mktemp -d)"
trap 'rm -rf "$task_build_dir"' EXIT
mkdir -p "$task_build_dir/classes" "$task_build_dir/dex"
"$java_root/bin/javac" -source 8 -target 8 \
  -cp "$sdk_root/platforms/android-36/android.jar" \
  -d "$task_build_dir/classes" "$project_root/tools/UiDump.java"
"$sdk_root/build-tools/36.0.0/d8" --min-api 28 \
  --lib "$sdk_root/platforms/android-36/android.jar" \
  --output "$task_build_dir/dex" "$task_build_dir/classes/xhs/UiDump.class"
output_path="${1:-$project_root/xiaohongshu/agent/ui-dump.dex}"
mkdir -p "$(dirname "$output_path")"
cp "$task_build_dir/dex/classes.dex" "$output_path"
