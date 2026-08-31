#!/usr/bin/env bash
# uninstall.sh — stop and remove housewatch's LaunchAgents.
#
# Removes BOTH the collector and (if installed) the menubar agent. They are
# installed separately — ./install.sh --with-menubar — but there is no reason
# to leave a menubar running against a collector that is gone, so uninstall
# takes both.
#
# By default this ONLY touches the LaunchAgent: it boots the service out of
# launchd and deletes the installed plist. Your config
# (~/.config/housewatch/config.toml), pidfile, and usage database are left
# exactly where they are — re-running install.sh later picks up right where
# you left off.
#
# Pass --purge to also permanently delete that data: the config file, the
# pidfile, and the SQLite database (plus its -wal/-shm siblings, wherever
# db_path in your config actually points). Log files under
# ~/Library/Logs/housewatch/ are never touched by this script, purge or not.
#
# Usage:
#   ./uninstall.sh            # stop + unload only
#   ./uninstall.sh --purge    # also delete config + pidfile + database
set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    -h|--help)
      echo "usage: $0 [--purge]"
      echo "  --purge   also delete config.toml, the pidfile, and the usage database"
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      echo "usage: $0 [--purge]" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
WORKDIR="${SCRIPT_DIR}"
HOME_DIR="${HOME}"
if [[ -n "${HOUSEWATCH_PYTHON:-}" ]]; then
  PYTHON="${HOUSEWATCH_PYTHON}"
elif [[ -x "${SCRIPT_DIR}/.venv/bin/python3" ]]; then
  PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
else
  PYTHON="$(command -v python3 || true)"
fi

LABEL="com.housewatch.collector"
LAUNCH_AGENTS_DIR="${HOME_DIR}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

CONFIG_DIR="${HOME_DIR}/.config/housewatch"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
PIDFILE="${CONFIG_DIR}/housewatch.pid"
LOG_DIR="${HOME_DIR}/Library/Logs/housewatch"

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

echo "== housewatch uninstall $( [[ "${PURGE}" -eq 1 ]] && echo '(--purge)' ) =="

# ---------------------------------------------------------------------------
# Resolve the configured db path BEFORE any config file is removed, so a
# --purge deletes wherever the user's config actually points db_path at,
# not just the hardcoded default location.
# ---------------------------------------------------------------------------
db_path=""
if [[ -x "${PYTHON}" ]]; then
  db_path="$(cd "${WORKDIR}" && "${PYTHON}" -c "
import os
from pathlib import Path
from housewatch import config
cfg_path = Path.home() / '.config' / 'housewatch' / 'config.toml'
if cfg_path.exists():
    try:
        cfg = config.load()
    except Exception:
        cfg = config.defaults()
else:
    cfg = config.defaults()
print(os.path.expanduser(cfg.db_path))
" 2>/dev/null || true)"
fi

# ---------------------------------------------------------------------------
# Stop + unload the agent
# ---------------------------------------------------------------------------
# The menubar is stopped FIRST: it offers Start/Stop Collector controls, so
# taking the collector out from under a live menubar would leave it showing
# stale numbers and a dead control for as long as it took to reach it here.
for _label in "com.housewatch.menubar" "${LABEL}"; do
  _target="${DOMAIN}/${_label}"
  _plist="${LAUNCH_AGENTS_DIR}/${_label}.plist"

  if launchctl print "${_target}" >/dev/null 2>&1; then
    echo "-- stopping ${_target} --"
    launchctl bootout "${_target}"
    echo "   stopped."
  else
    echo "-- ${_target} is not loaded, nothing to stop --"
  fi

  if [[ -f "${_plist}" ]]; then
    rm -f "${_plist}"
    echo "-- removed ${_plist} --"
  else
    echo "-- ${_plist} does not exist, nothing to remove --"
  fi
done

echo
if [[ "${PURGE}" -eq 0 ]]; then
  echo "Left in place (re-run with --purge to remove):"
  echo "  config:   ${CONFIG_FILE}"
  if [[ -n "${db_path}" ]]; then
    echo "  database: ${db_path} (+ -wal/-shm)"
  fi
  echo "  pidfile:  ${PIDFILE}"
  echo "  logs:     ${LOG_DIR}/ (never touched by this script, purge or not)"
else
  echo "-- --purge: removing config + pidfile + database --"
  if [[ -n "${db_path}" ]]; then
    for f in "${db_path}" "${db_path}-wal" "${db_path}-shm"; do
      if [[ -f "${f}" ]]; then
        rm -f "${f}"
        echo "  removed ${f}"
      fi
    done
  else
    echo "  (could not resolve db_path — venv python at ${PYTHON} unavailable; database NOT removed)"
  fi
  if [[ -f "${CONFIG_FILE}" ]]; then
    rm -f "${CONFIG_FILE}"
    echo "  removed ${CONFIG_FILE}"
  fi
  if [[ -f "${PIDFILE}" ]]; then
    rm -f "${PIDFILE}"
    echo "  removed ${PIDFILE}"
  fi
  if [[ -d "${CONFIG_DIR}" ]] && [[ -z "$(ls -A "${CONFIG_DIR}" 2>/dev/null)" ]]; then
    rmdir "${CONFIG_DIR}"
    echo "  removed empty directory ${CONFIG_DIR}"
  fi
  echo "  logs at ${LOG_DIR}/ were NOT removed (not requested by --purge)."
fi

echo
echo "Done."
