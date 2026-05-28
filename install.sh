#!/usr/bin/env bash
# auto-pr-skill installer
#
# Installs the OpenCode skill, /auto-pr slash command, the auto-pr-* subagents,
# and project profiles into OpenCode's global config and/or a specific
# project's .opencode/ directory.
#
# Usage:
#   ./install.sh                            # global only
#   ./install.sh --project /path/to/repo    # global + per-project (Paddle, etc.)
#   ./install.sh --project P --project-only # per-project only, no global links
#   ./install.sh --uninstall                # remove global symlinks
#   ./install.sh --uninstall --project P    # also remove project symlinks
#   ./install.sh --uninstall --project P --project-only # remove project only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_PATH=""
UNINSTALL=0
PROJECT_ONLY=0

GLOBAL_OPENCODE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
GLOBAL_PROFILE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/auto-pr"

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'
    RED=$'\033[0;31m'
    BOLD=$'\033[1m'
    RESET=$'\033[0m'
else
    GREEN="" YELLOW="" RED="" BOLD="" RESET=""
fi

log()  { printf '%s[auto-pr-skill]%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '%s[ok]%s %s\n'           "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n'         "$YELLOW" "$RESET" "$*"; }
err()  { printf '%s[err]%s %s\n'          "$RED"   "$RESET" "$*" >&2; }

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            [[ $# -ge 2 ]] || { err "--project requires a path"; exit 2; }
            PROJECT_PATH="$(cd "$2" && pwd)"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --project-only|--no-global)
            PROJECT_ONLY=1
            shift
            ;;
        -h|--help)
            usage 0
            ;;
        *)
            err "Unknown argument: $1"
            usage 2
            ;;
    esac
done

if (( PROJECT_ONLY )) && [[ -z "$PROJECT_PATH" ]]; then
    err "--project-only requires --project /path/to/repo"
    exit 2
fi

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_prereqs() {
    local missing=()
    for tool in git python3 jq gh; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done

    if ! command -v opencode >/dev/null 2>&1; then
        warn "'opencode' CLI not found in PATH. The skill files will still install, but you'll need OpenCode (https://opencode.ai) to use them."
    fi

    if (( ${#missing[@]} > 0 )); then
        err "Missing required tools: ${missing[*]}"
        err "Install them and re-run."
        exit 3
    fi
    ok "Prerequisites OK (git, python3, jq, gh)"
}

# ---------------------------------------------------------------------------
# Symlink helpers
# ---------------------------------------------------------------------------
link() {
    local src="$1" dst="$2"
    mkdir -p "$(dirname "$dst")"
    if [[ -L "$dst" ]]; then
        rm -f "$dst"
    elif [[ -e "$dst" ]]; then
        local backup="${dst}.bak.$(date +%s)"
        warn "Backing up existing $dst -> $backup"
        mv "$dst" "$backup"
    fi
    ln -s "$src" "$dst"
    ok "linked $dst -> $src"
}

unlink_if_ours() {
    local dst="$1" expected_prefix="$2"
    if [[ -L "$dst" ]]; then
        local target
        target="$(readlink "$dst")"
        if [[ "$target" == "$expected_prefix"* ]]; then
            rm -f "$dst"
            ok "removed $dst"
        else
            warn "skipped $dst (not ours: -> $target)"
        fi
    elif [[ -e "$dst" ]]; then
        warn "skipped $dst (not a symlink)"
    fi
}

ensure_local_artifact_ignores() {
    local proj="$1"
    local git_dir exclude_file pattern missing=()

    if ! git -C "$proj" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        warn "Skipping local git excludes for $proj (not a git worktree)"
        return 0
    fi

    git_dir="$(git -C "$proj" rev-parse --absolute-git-dir)"
    exclude_file="$git_dir/info/exclude"
    mkdir -p "$(dirname "$exclude_file")"
    touch "$exclude_file"

    for pattern in "/.auto-pr/" "/.opencode/"; do
        if ! grep -Fxq "$pattern" "$exclude_file"; then
            missing+=("$pattern")
        fi
    done
    (( ${#missing[@]} == 0 )) && return 0

    {
        printf '\n# auto-pr-skill local artifacts\n'
        printf '%s\n' "${missing[@]}"
    } >> "$exclude_file"
    ok "ignored auto-pr local artifacts in $exclude_file"
}

# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------
install_global() {
    log "Installing globally into $GLOBAL_OPENCODE_DIR"

    link "$SCRIPT_DIR/SKILL.md" \
         "$GLOBAL_OPENCODE_DIR/skills/auto-pr-skill/SKILL.md"

    link "$SCRIPT_DIR/opencode/commands/auto-pr.md" \
         "$GLOBAL_OPENCODE_DIR/commands/auto-pr.md"

    for f in "$SCRIPT_DIR"/opencode/agents/*.md; do
        link "$f" "$GLOBAL_OPENCODE_DIR/agents/$(basename "$f")"
    done

    mkdir -p "$GLOBAL_PROFILE_DIR/projects"
    for f in "$SCRIPT_DIR"/projects/*.yaml; do
        [[ -e "$f" ]] || continue
        link "$f" "$GLOBAL_PROFILE_DIR/projects/$(basename "$f")"
    done

    link "$SCRIPT_DIR/lib"       "$GLOBAL_PROFILE_DIR/lib"
    link "$SCRIPT_DIR/templates" "$GLOBAL_PROFILE_DIR/templates"
}

uninstall_global() {
    log "Uninstalling globally from $GLOBAL_OPENCODE_DIR"
    unlink_if_ours "$GLOBAL_OPENCODE_DIR/skills/auto-pr-skill/SKILL.md" "$SCRIPT_DIR/"
    unlink_if_ours "$GLOBAL_OPENCODE_DIR/commands/auto-pr.md" "$SCRIPT_DIR/"

    if [[ -d "$GLOBAL_OPENCODE_DIR/agents" ]]; then
        for f in "$GLOBAL_OPENCODE_DIR/agents"/auto-pr-*.md; do
            [[ -e "$f" || -L "$f" ]] || continue
            unlink_if_ours "$f" "$SCRIPT_DIR/"
        done
    fi

    if [[ -d "$GLOBAL_PROFILE_DIR/projects" ]]; then
        for f in "$GLOBAL_PROFILE_DIR/projects"/*.yaml; do
            [[ -e "$f" || -L "$f" ]] || continue
            unlink_if_ours "$f" "$SCRIPT_DIR/"
        done
    fi
    unlink_if_ours "$GLOBAL_PROFILE_DIR/lib"       "$SCRIPT_DIR/"
    unlink_if_ours "$GLOBAL_PROFILE_DIR/templates" "$SCRIPT_DIR/"
}

# Pick the profile yaml that matches the given project directory.
# Heuristic: match by basename (case-insensitive) of the project path
# against a profile's `name` field; fall back to the dir basename.
pick_profile_for_project() {
    local proj="$1"
    local name
    name="$(basename "$proj" | tr '[:upper:]' '[:lower:]')"

    for f in "$SCRIPT_DIR"/projects/*.yaml; do
        [[ -e "$f" ]] || continue
        local pname
        pname="$(awk -F': *' '/^name:/ {print tolower($2); exit}' "$f")"
        if [[ "$pname" == "$name" ]]; then
            echo "$f"
            return 0
        fi
    done
    return 1
}

install_project() {
    local proj="$1"
    log "Installing into project $proj"

    local proj_oc="$proj/.opencode"
    local proj_ap="$proj/.auto-pr"

    ensure_local_artifact_ignores "$proj"

    link "$SCRIPT_DIR/SKILL.md" \
         "$proj_oc/skills/auto-pr-skill/SKILL.md"

    for f in "$SCRIPT_DIR"/opencode/agents/*.md; do
        link "$f" "$proj_oc/agents/$(basename "$f")"
    done
    link "$SCRIPT_DIR/opencode/commands/auto-pr.md" \
         "$proj_oc/commands/auto-pr.md"

    local profile
    if profile="$(pick_profile_for_project "$proj")"; then
        link "$profile" "$proj_ap/profile.yaml"
    else
        warn "No matching profile in projects/*.yaml for $(basename "$proj"). Create one and re-run, or place it manually at $proj_ap/profile.yaml."
    fi
}

uninstall_project() {
    local proj="$1"
    log "Uninstalling from project $proj"
    if [[ -d "$proj/.opencode/agents" ]]; then
        for f in "$proj/.opencode/agents"/auto-pr-*.md; do
            [[ -e "$f" || -L "$f" ]] || continue
            unlink_if_ours "$f" "$SCRIPT_DIR/"
        done
    fi
    unlink_if_ours "$proj/.opencode/skills/auto-pr-skill/SKILL.md" "$SCRIPT_DIR/"
    unlink_if_ours "$proj/.opencode/commands/auto-pr.md" "$SCRIPT_DIR/"
    unlink_if_ours "$proj/.auto-pr/profile.yaml"         "$SCRIPT_DIR/"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
check_prereqs

if (( UNINSTALL )); then
    if (( ! PROJECT_ONLY )); then
        uninstall_global
    fi
    [[ -n "$PROJECT_PATH" ]] && uninstall_project "$PROJECT_PATH"
    ok "Done."
    exit 0
fi

if (( ! PROJECT_ONLY )); then
    install_global
fi
[[ -n "$PROJECT_PATH" ]] && install_project "$PROJECT_PATH"

cat <<EOF

${GREEN}auto-pr-skill installed.${RESET}

Try it from inside any repo with a profile installed:

    ${BOLD}opencode${RESET}            # then type:
    ${BOLD}/auto-pr <project-name>${RESET}

Or, if installed per-project:

    cd ${PROJECT_PATH:-/path/to/repo}
    opencode
    /auto-pr $(basename "${PROJECT_PATH:-paddle}")

Profiles live in:  ${BOLD}$SCRIPT_DIR/projects/*.yaml${RESET}
Add a new project: copy paddle.yaml, edit name/repo_path/build_cmd, re-run install.

EOF
