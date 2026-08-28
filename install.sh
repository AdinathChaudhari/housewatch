#!/usr/bin/env bash
# install.sh — install housewatch's collector as a per-user LaunchAgent.
#
# Idempotent: safe to re-run any time (e.g. after `git pull`, or to pick up
# a config change) — it always boots out any existing instance before
# reloading, so double-invoking this never leaves two collectors racing for
# the single-writer db lock.
#
# What this does, in order:
#   1. Resolve paths (this script's own directory is treated as the
#      housewatch repo root — WorkingDirectory for the agent) and the
#      interpreter (HOUSEWATCH_PYTHON, else this repo's own .venv, else
#      whatever python3 is on PATH — never a hardcoded personal path).
#   2. Verify that interpreter exists and can actually import housewatch
#      and run `python -m housewatch --help` — BEFORE touching launchd, so a
#      broken checkout fails loudly here instead of flapping silently under
#      KeepAlive.
#   3. Report the config state. An existing ~/.config/housewatch/config.toml
#      is never touched or seeded — a fresh install runs on housewatch's own
#      built-in defaults (no data cap set) until you set one.
#   4. Render the LaunchAgent plist template (substitute {PYTHON}/{WORKDIR}/
#      {HOME} with real absolute paths) and write it to
#      ~/Library/LaunchAgents/.
#   5. Boot out any existing instance of the agent, then bootstrap the new
#      plist.
#   6. Verify it's actually running and print real status plus the next
#      command to run.
#
# NOTE: the rendered plist embeds WORKDIR as an absolute path resolved from
# wherever THIS script lives when you run it. If the housewatch checkout
# later moves, just re-run this script from the new location — it re-renders
# and reloads the plist with the new path. There is nothing to hand-edit.
set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
WORKDIR="${SCRIPT_DIR}"
HOME_DIR="${HOME}"
if [[ -n "${HOUSEWATCH_PYTHON:-}" ]]; then
  PYTHON="${HOUSEWATCH_PYTHON}"
elif [[ -x "${SCRIPT_DIR}/.venv/bin/python3" ]]; then
  PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "ERROR: no python3 found on PATH, and no venv at ${SCRIPT_DIR}/.venv." >&2
  echo "  fix: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'," >&2
  echo "  or set HOUSEWATCH_PYTHON=/path/to/python3 and re-run this script." >&2
  exit 1
fi

LABEL="com.housewatch.collector"
PLIST_TEMPLATE="${WORKDIR}/com.housewatch.collector.plist"
LAUNCH_AGENTS_DIR="${HOME_DIR}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

CONFIG_DIR="${HOME_DIR}/.config/housewatch"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
CONFIG_EXAMPLE="${WORKDIR}/config.example.toml"

LOG_DIR="${HOME_DIR}/Library/Logs/housewatch"

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

echo "== housewatch install =="
echo "  repo:   ${WORKDIR}"
echo "  python: ${PYTHON}"
echo

# ---------------------------------------------------------------------------
# 2. Verify the venv python + package BEFORE touching launchd. A broken
#    install must fail loudly here, not flap silently in launchd.
# ---------------------------------------------------------------------------
if [[ ! -x "${PYTHON}" ]]; then
  echo "ERROR: venv python not found or not executable: ${PYTHON}" >&2
  echo "  fix: install it, or set HOUSEWATCH_PYTHON=/path/to/python3 and re-run this script." >&2
  exit 1
fi

echo "-- verifying ${PYTHON} can import housewatch from ${WORKDIR} --"
if ! (cd "${WORKDIR}" && "${PYTHON}" -c "import housewatch.cli"); then
  echo "ERROR: '${PYTHON} -c \"import housewatch.cli\"' failed from ${WORKDIR}." >&2
  echo "  A LaunchAgent built on this would fail on every start and KeepAlive" >&2
  echo "  would relaunch it forever. Fix the import error above, then re-run install.sh." >&2
  exit 1
fi
if ! (cd "${WORKDIR}" && "${PYTHON}" -m housewatch --help >/dev/null); then
  echo "ERROR: '${PYTHON} -m housewatch --help' failed from ${WORKDIR}." >&2
  exit 1
fi
echo "   OK — housewatch imports and \`python -m housewatch\` runs."
echo

# ---------------------------------------------------------------------------
# 3. Report config state (never write one — a copied cap/timezone that was
#    right for whoever wrote config.example.toml is silently wrong for
#    anyone else's house).
# ---------------------------------------------------------------------------
if [[ -f "${CONFIG_FILE}" ]]; then
  echo "-- config: ${CONFIG_FILE} already exists — leaving it untouched --"
else
  echo "-- config: none — running on built-in defaults (no data cap set)."
  echo "   Set one any time: ${PYTHON} -m housewatch config set-cap <GB>"
  echo "   Or copy the annotated template: mkdir -p ${CONFIG_DIR} && cp ${CONFIG_EXAMPLE} ${CONFIG_FILE}"
fi
echo

# ---------------------------------------------------------------------------
# 4. Render + write the plist
# ---------------------------------------------------------------------------
if [[ ! -f "${PLIST_TEMPLATE}" ]]; then
  echo "ERROR: plist template not found: ${PLIST_TEMPLATE}" >&2
  exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}"
mkdir -p "${LOG_DIR}"

_sed_escape() {
  # Escape backslash, ampersand, and '#' (our sed delimiter below) so an
  # arbitrary absolute path can be dropped into a sed replacement without
  # any special character in it being reinterpreted.
  printf '%s' "$1" | sed -e 's/[\&#]/\\&/g'
}

_esc_python="$(_sed_escape "${PYTHON}")"
_esc_workdir="$(_sed_escape "${WORKDIR}")"
_esc_home="$(_sed_escape "${HOME_DIR}")"

TMP_PLIST="$(mktemp -t housewatch_plist)"
trap 'rm -f "${TMP_PLIST}"' EXIT

sed \
  -e "s#{PYTHON}#${_esc_python}#g" \
  -e "s#{WORKDIR}#${_esc_workdir}#g" \
  -e "s#{HOME}#${_esc_home}#g" \
  "${PLIST_TEMPLATE}" > "${TMP_PLIST}"

if command -v plutil >/dev/null 2>&1; then
  if ! plutil -lint "${TMP_PLIST}"; then
    echo "ERROR: rendered plist failed plutil -lint (see above): ${TMP_PLIST}" >&2
    exit 1
  fi
else
  echo "-- plutil not found; skipping plist lint --"
fi

chmod 644 "${TMP_PLIST}"
mv "${TMP_PLIST}" "${PLIST_DEST}"
trap - EXIT
echo "-- wrote ${PLIST_DEST} --"
echo

# ---------------------------------------------------------------------------
# 5. (Re)load — bootout any existing instance first so re-running this is
#    always safe, then bootstrap the freshly rendered plist.
# ---------------------------------------------------------------------------
echo "-- stopping any existing instance (safe if none is loaded) --"
launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true

echo "-- bootstrapping ${SERVICE_TARGET} --"
launchctl bootstrap "${DOMAIN}" "${PLIST_DEST}"
echo

# ---------------------------------------------------------------------------
# 6. Verify + report real status
# ---------------------------------------------------------------------------
running=0
for _ in 1 2 3; do
  if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
    running=1
    break
  fi
  sleep 1
done

if [[ "${running}" -eq 1 ]]; then
  echo "== OK: ${LABEL} is loaded =="
  launchctl print "${SERVICE_TARGET}" | grep -E 'state =|pid =|program =|last exit' || true
else
  echo "== WARNING: bootstrap succeeded but launchctl print can't find ${SERVICE_TARGET} ==" >&2
  echo "   check ${LOG_DIR}/collector.err.log for a crash on startup." >&2
fi

echo
echo "Next: ${PYTHON} -m housewatch status"
echo "  or: make status   (from ${WORKDIR})"
