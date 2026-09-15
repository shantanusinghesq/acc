#!/usr/bin/env bash
# Install the acc skill for Claude Code (macOS / Linux).
#
# Symlinks this checkout into your Claude skills dir so `/acc` resolves in any
# project, then verifies the install. Re-runnable (idempotent).
#
#   ./install.sh            # symlink (recommended; edits here take effect live)
#   ./install.sh --copy     # copy files instead of symlinking
#   CLAUDE_SKILLS_DIR=... ./install.sh   # override the skills dir
#
# Uninstall with: make uninstall   (or remove the symlink it reports below)
set -euo pipefail

SRC="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/acc"
MODE="symlink"
[ "${1:-}" = "--copy" ] && MODE="copy"

# Return the physical path of a directory entry without following the entry
# itself. Resolving only its nearest existing parent lets us safely inspect a
# missing destination and preserves the normal case where DEST is a symlink
# that can be unlinked and recreated.
canonical_entry_path() {
  local path=$1 parent leaf suffix component next_parent physical_parent
  case "$path" in
    /*) ;;
    *) path="$PWD/$path" ;;
  esac

  parent=$(dirname "$path")
  leaf=$(basename "$path")
  suffix=""
  while [ ! -d "$parent" ]; do
    component=$(basename "$parent")
    suffix="/$component$suffix"
    next_parent=$(dirname "$parent")
    if [ "$next_parent" = "$parent" ]; then
      echo "error: cannot resolve parent of $path" >&2
      return 1
    fi
    parent=$next_parent
  done

  physical_parent=$(cd -P "$parent" && pwd -P)
  printf '%s%s/%s\n' "${physical_parent%/}" "$suffix" "$leaf"
}

path_contains() {
  local parent=${1%/}
  local child=${2%/}
  [ "$parent" = "$child" ] || [ "${child#"$parent"/}" != "$child" ]
}

# On a case-insensitive filesystem two spellings can identify the same entry
# even when their strings differ. Walk both existing directory ancestries and
# compare filesystem identities. Do not follow the DEST leaf when it is a
# symlink: unlinking and recreating that directory entry is a safe reinstall.
filesystem_paths_overlap() {
  local current next
  [ -L "$DEST" ] && return 1
  [ -d "$DEST" ] || return 1

  current=$SRC
  while :; do
    [ "$current" -ef "$DEST" ] && return 0
    next=$(dirname "$current")
    [ "$next" = "$current" ] && break
    current=$next
  done

  current=$(cd -P "$DEST" && pwd -P)
  while :; do
    [ "$current" -ef "$SRC" ] && return 0
    next=$(dirname "$current")
    [ "$next" = "$current" ] && break
    current=$next
  done
  return 1
}

DEST_ENTRY=$(canonical_entry_path "$DEST")

# Reject identity and overlap before creating or deleting anything. Either
# direction is unsafe in copy mode: removing an ancestor destroys the source,
# while copying into a descendant recursively consumes its own output. The
# same guard also keeps symlink installs from placing the link inside SRC.
if path_contains "$SRC" "$DEST_ENTRY" ||
  path_contains "$DEST_ENTRY" "$SRC" ||
  filesystem_paths_overlap; then
  echo "error: refusing to install because source and destination overlap." >&2
  echo "       source:      $SRC" >&2
  echo "       destination: $DEST_ENTRY" >&2
  exit 1
fi

if [ ! -f "$SRC/SKILL.md" ]; then
  echo "error: $SRC doesn't look like the acc skill (no SKILL.md)" >&2
  exit 1
fi

mkdir -p "$SKILLS_DIR"

# Clear a prior install. A symlink is always ours to replace; a real
# directory is only replaced in --copy mode (a prior copy install), keeping
# the command idempotent without clobbering something unexpected.
if [ -L "$DEST" ]; then
  rm "$DEST"
elif [ -d "$DEST" ] && [ "$MODE" = "copy" ]; then
  rm -rf "$DEST"
elif [ -e "$DEST" ]; then
  echo "error: $DEST already exists and is not replaceable in $MODE mode." >&2
  echo "       Remove it first if you want to reinstall." >&2
  exit 1
fi

if [ "$MODE" = "copy" ]; then
  cp -R "$SRC" "$DEST"
else
  ln -s "$SRC" "$DEST"
fi

# Verify the loader will find the entry point.
if [ -f "$DEST/SKILL.md" ]; then
  echo "Installed acc ($MODE) -> $DEST"
  echo "Open Claude Code in any project and run /acc to confirm."
else
  echo "error: install verification failed; $DEST/SKILL.md missing" >&2
  exit 1
fi
