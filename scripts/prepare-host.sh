#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host_dir="$repo_root/host/MaaTauriAndroid"
patch_file="$repo_root/patches/maa-tauri-android.patch"
git -C "$repo_root" submodule update --init host/MaaTauriAndroid
if git -C "$host_dir" apply --reverse --check "$patch_file" 2>/dev/null; then
  echo 'Host patch already applied.'
else
  git -C "$host_dir" apply --check "$patch_file"
  git -C "$host_dir" apply "$patch_file"
  echo 'Host patch applied.'
fi
