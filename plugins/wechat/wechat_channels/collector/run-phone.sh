#!/system/bin/sh
set -eu
ROOT=/data/local/tmp/wechat-channels-dev
export PYTHONHOME="$ROOT/runtime/prefix"
export PYTHONPATH="$ROOT/runtime/site-packages:$ROOT/pi/collector"
export MAAFW_BINARY_PATH="$ROOT/runtime/lib"
export LD_LIBRARY_PATH="$ROOT/runtime/prefix/lib:$ROOT/runtime/lib:$ROOT/runtime/site-packages/numpy.libs"
exec "$ROOT/runtime/bin/python3" "$ROOT/pi/collector/phone.py" "$ROOT" "$@"
