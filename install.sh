#!/usr/bin/env bash
# install.sh — install housewatch's LaunchAgents.
#
# By default this installs the COLLECTOR only — the background daemon that
# polls the router. The MENUBAR app is optional and opt-in:
#
#   ./install.sh                  collector only (the default)
#   ./install.sh --with-menubar   collector + menubar app
#   ./install.sh --menubar-only   menubar app only (collector left alone)
#
# They are two separate agents on purpose. The collector holds the single
# writer lock on the database and must never be interrupted by a UI problem;
# the menubar draws an NSStatusItem through rumps/PyObjC and is the more
# fragile of the two. Keeping them apart means a crashed or quit menubar can
# never cost you data, and restarting one never touches the other.
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
#      KeepAlive. For the menubar, additionally verify rumps imports.
#   3. Report the config state. An existing ~/.config/housewatch/config.toml
#      is never touched or seeded — a fresh install runs on housewatch's own
#      built-in defaults (no data cap set) until you set one.
#   4. For each agent selected: render its plist template (substitute
#      {PYTHON}/{WORKDIR}/{HOME} with real absolute paths), write it to
#      ~/Library/LaunchAgents/, boot out any existing instance, bootstrap the
#      new one, and verify it is actually running.
#
# NOTE: the rendered plists embed WORKDIR as an absolute path resolved from
# wherever THIS script lives when you run it. If the housewatch checkout
# later moves, just re-run this script from the new location — it re-renders
# and reloads with the new path. There is nothing to hand-edit.

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Arguments
# ---------------------------------------------------------------------------
WANT_COLLECTOR=1
WANT_MENUBAR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-menubar) WANT_MENUBAR=1 ;;
    --menubar-only) WANT_MENUBAR=1; WANT_COLLECTOR=0 ;;
    -h|--help)
      sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      echo "  usage: ./install.sh [--with-menubar | --menubar-only]" >&2
      exit 2
      ;;
  esac
  shift
done

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

COLLECTOR_LABEL="com.housewatch.collector"
COLLECTOR_TEMPLATE="${WORKDIR}/com.housewatch.collector.plist"
MENUBAR_LABEL="com.housewatch.menubar"
MENUBAR_TEMPLATE="${WORKDIR}/housewatch/menubar_launchagent.plist"

LAUNCH_AGENTS_DIR="${HOME_DIR}/Library/LaunchAgents"

CONFIG_DIR="${HOME_DIR}/.config/housewatch"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
CONFIG_EXAMPLE="${WORKDIR}/config.example.toml"

LOG_DIR="${HOME_DIR}/Library/Logs/housewatch"

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

echo "== housewatch install =="
echo "  repo:   ${WORKDIR}"
echo "  python: ${PYTHON}"
_selected="collector"
[[ "${WANT_COLLECTOR}" -eq 0 ]] && _selected="menubar"
[[ "${WANT_COLLECTOR}" -eq 1 && "${WANT_MENUBAR}" -eq 1 ]] && _selected="collector + menubar"
echo "  agents: ${_selected}"
echo

# ---------------------------------------------------------------------------
# 2. Verify the python + package BEFORE touching launchd. A broken install
#    must fail loudly here, not flap silently in launchd.
# ---------------------------------------------------------------------------
if [[ ! -x "${PYTHON}" ]]; then
  echo "ERROR: python not found or not executable: ${PYTHON}" >&2
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

# rumps is an extra dependency and only the menubar needs it. Check it only
# when the menubar was actually asked for, so a headless/server install is
# never blocked by a GUI library it will not use.
if [[ "${WANT_MENUBAR}" -eq 1 ]]; then
  echo "-- verifying rumps (menubar) --"
  if ! (cd "${WORKDIR}" && "${PYTHON}" -c "import rumps, housewatch.menubar"); then
    echo "ERROR: the menubar app needs rumps, and importing it failed." >&2
    echo "  fix: ${PYTHON} -m pip install rumps" >&2
    echo "  (the collector does NOT need rumps — ./install.sh with no flags still works.)" >&2
    exit 1
  fi
  echo "   OK — rumps and housewatch.menubar import."
fi
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

# ---------------------------------------------------------------------------
# 4. Render, install, (re)load and verify ONE agent.
#      install_agent <label> <template-path> <log-basename>
#    Used for both agents so they cannot drift apart in behaviour — the
#    bootout-before-bootstrap ordering in particular is what makes re-running
#    this script safe rather than leaving two instances racing.
# ---------------------------------------------------------------------------
install_agent() {
  local label="$1" template="$2" logname="$3"
  local dest="${LAUNCH_AGENTS_DIR}/${label}.plist"
  local target="${DOMAIN}/${label}"

  echo "== ${label} =="

  if [[ ! -f "${template}" ]]; then
    echo "ERROR: plist template not found: ${template}" >&2
    return 1
  fi

  local tmp
  tmp="$(mktemp -t housewatch_plist)"

  sed \
    -e "s#{PYTHON}#${_esc_python}#g" \
    -e "s#{WORKDIR}#${_esc_workdir}#g" \
    -e "s#{HOME}#${_esc_home}#g" \
    "${template}" > "${tmp}"

  if command -v plutil >/dev/null 2>&1; then
    if ! plutil -lint "${tmp}"; then
      echo "ERROR: rendered plist failed plutil -lint (see above): ${tmp}" >&2
      rm -f "${tmp}"
      return 1
    fi
  else
    echo "-- plutil not found; skipping plist lint --"
  fi

  chmod 644 "${tmp}"
  mv "${tmp}" "${dest}"
  echo "-- wrote ${dest} --"

  echo "-- stopping any existing instance (safe if none is loaded) --"
  launchctl bootout "${target}" 2>/dev/null || true

  # bootout is ASYNCHRONOUS. It returns before launchd has finished tearing
  # the job down, and bootstrapping into a domain that still holds the old
  # job fails with the famously unhelpful "Bootstrap failed: 5: Input/output
  # error". Wait for the service to actually disappear rather than sleeping a
  # fixed amount and hoping.
  local gone=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! launchctl print "${target}" >/dev/null 2>&1; then
      gone=1
      break
    fi
    sleep 0.5
  done
  if [[ "${gone}" -eq 0 ]]; then
    echo "WARNING: ${target} is still registered 5s after bootout;" >&2
    echo "  bootstrapping anyway — if this fails with 'Input/output error'," >&2
    echo "  re-run ./install.sh in a moment." >&2
  fi

  echo "-- bootstrapping ${target} --"
  launchctl bootstrap "${DOMAIN}" "${dest}"

  local running=0
  for _ in 1 2 3; do
    if launchctl print "${target}" >/dev/null 2>&1; then
      running=1
      break
    fi
    sleep 1
  done

  if [[ "${running}" -eq 1 ]]; then
    echo "== OK: ${label} is loaded =="
    launchctl print "${target}" | grep -E 'state =|pid =|program =|last exit' || true
  else
    echo "== WARNING: bootstrap succeeded but launchctl print can't find ${target} ==" >&2
    echo "   check ${LOG_DIR}/${logname}.err.log for a crash on startup." >&2
  fi
  echo
}

# ---------------------------------------------------------------------------
# 5. Install what was asked for
# ---------------------------------------------------------------------------
if [[ "${WANT_COLLECTOR}" -eq 1 ]]; then
  install_agent "${COLLECTOR_LABEL}" "${COLLECTOR_TEMPLATE}" "collector"
fi

if [[ "${WANT_MENUBAR}" -eq 1 ]]; then
  install_agent "${MENUBAR_LABEL}" "${MENUBAR_TEMPLATE}" "menubar"
fi

# ---------------------------------------------------------------------------
# 6. What to do next
# ---------------------------------------------------------------------------
echo "Next: ${PYTHON} -m housewatch status"
echo "  or: make status   (from ${WORKDIR})"
if [[ "${WANT_MENUBAR}" -eq 1 ]]; then
  echo
  echo "The menubar app should now be in your menu bar — it shows today's usage,"
  echo "or cycle progress once a cap is set. Its 'Open Dashboard' item starts the"
  echo "web UI for you, so you never need to leave a terminal running."
elif [[ "${WANT_COLLECTOR}" -eq 1 ]]; then
  echo
  echo "Want a menu-bar readout too?  ./install.sh --with-menubar"
fi
