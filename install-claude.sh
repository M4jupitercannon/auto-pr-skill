#!/usr/bin/env bash
# auto-pr-skill — Claude Code installer (+ optional DeepSeek migration)
#
# Installs the auto-pr skill, the /auto-pr slash command, and the auto-pr-*
# subagents into Claude Code's global config (~/.claude) and/or a specific
# project's .claude/ directory. The shared lib/templates/references/profiles
# are linked into ~/.config/auto-pr so the agents resolve them at runtime.
#
# It can also perform the DeepSeek "migrate to Anthropic API" setup described
# at https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code
# by writing a sourceable env file that points Claude Code at DeepSeek models.
#
# Usage:
#   ./install-claude.sh                              # global install + DeepSeek env file
#   ./install-claude.sh --project /path/to/repo      # global + per-project (.claude/)
#   ./install-claude.sh --project P --project-only   # per-project only, no global links
#   ./install-claude.sh --no-deepseek                # skip the DeepSeek env file
#   ./install-claude.sh --deepseek --api-key sk-...  # write env file with your key
#   ./install-claude.sh --persist                    # also append `source` to shell rc
#   ./install-claude.sh --install-cli                # npm install -g @anthropic-ai/claude-code
#   ./install-claude.sh --uninstall                  # remove global symlinks
#   ./install-claude.sh --uninstall --project P      # also remove project symlinks
#
# Flags:
#   --project PATH       install per-project into <repo>/.claude and <repo>/.auto-pr
#   --project-only       skip the global (~/.claude) install
#   --uninstall          remove the symlinks this installer created
#   --deepseek           write the DeepSeek env file (default: on)
#   --no-deepseek        do not touch any DeepSeek env file
#   --api-key KEY        DeepSeek API key (else $DEEPSEEK_API_KEY, else a placeholder)
#   --env-file PATH      where to write the env file (default ~/.config/auto-pr/claude-deepseek.env)
#   --persist            append a guarded `source <env-file>` block to your shell rc
#   --shell-rc PATH      shell rc to use with --persist (default: autodetect)
#   --install-cli        run `npm install -g @anthropic-ai/claude-code` first
#   -h, --help           show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_PATH=""
UNINSTALL=0
PROJECT_ONLY=0
DEEPSEEK=1
PERSIST=0
INSTALL_CLI=0
API_KEY="${DEEPSEEK_API_KEY:-}"
ENV_FILE=""
SHELL_RC=""

GLOBAL_CLAUDE_DIR="$HOME/.claude"
GLOBAL_PROFILE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/auto-pr"

# DeepSeek -> Claude Code (Anthropic API) model migration, per DeepSeek docs.
DEEPSEEK_BASE_URL="https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL="deepseek-v4-pro[1m]"
DEEPSEEK_FAST_MODEL="deepseek-v4-flash"

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
    sed -n '/^# Usage:/,/^# *-h, --help/p' "$0" | sed 's/^# \{0,1\}//'
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
        --project-only|--no-global)
            PROJECT_ONLY=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --deepseek)
            DEEPSEEK=1
            shift
            ;;
        --no-deepseek)
            DEEPSEEK=0
            shift
            ;;
        --api-key)
            [[ $# -ge 2 ]] || { err "--api-key requires a value"; exit 2; }
            API_KEY="$2"
            shift 2
            ;;
        --env-file)
            [[ $# -ge 2 ]] || { err "--env-file requires a path"; exit 2; }
            ENV_FILE="$2"
            shift 2
            ;;
        --persist)
            PERSIST=1
            shift
            ;;
        --shell-rc)
            [[ $# -ge 2 ]] || { err "--shell-rc requires a path"; exit 2; }
            SHELL_RC="$2"
            shift 2
            ;;
        --install-cli)
            INSTALL_CLI=1
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

ENV_FILE="${ENV_FILE:-$GLOBAL_PROFILE_DIR/claude-deepseek.env}"

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_prereqs() {
    local missing=()
    for tool in git python3 jq gh; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done

    if ! command -v claude >/dev/null 2>&1; then
        warn "'claude' CLI not found in PATH. Skill files will still install; run with --install-cli or 'npm install -g @anthropic-ai/claude-code' to get Claude Code."
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

# Merge the auto-pr guardrails into a Claude Code settings.json without
# clobbering the user's other settings (deny/allow arrays are unioned).
merge_settings() {
    local target="$1"
    local src="$SCRIPT_DIR/claude/settings.json"
    [[ -f "$src" ]] || return 0
    mkdir -p "$(dirname "$target")"
    [[ -f "$target" ]] || echo '{}' > "$target"
    cp "$target" "$target.bak.$(date +%s)"
    local tmp
    tmp="$(mktemp)"
    if jq -s '
        .[0] as $cur | .[1] as $ours
        | $cur
        | .permissions = ($cur.permissions // {})
        | .permissions.deny  = ((($cur.permissions.deny  // []) + ($ours.permissions.deny  // [])) | unique)
        | .permissions.allow = ((($cur.permissions.allow // []) + ($ours.permissions.allow // [])) | unique)
    ' "$target" "$src" > "$tmp"; then
        mv "$tmp" "$target"
        ok "merged auto-pr guardrails into $target"
    else
        rm -f "$tmp"
        warn "could not merge guardrails into $target (left unchanged)"
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

    for pattern in "/.auto-pr/" "/.claude/"; do
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
# Claude CLI install + DeepSeek migration
# ---------------------------------------------------------------------------
install_cli() {
    log "Installing Claude Code CLI via npm"
    if ! command -v npm >/dev/null 2>&1; then
        err "npm not found. Install Node.js 18+ first (https://nodejs.org), then re-run with --install-cli."
        exit 4
    fi
    if command -v node >/dev/null 2>&1; then
        local major
        major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
        if (( major < 18 )); then
            warn "Node $(node --version) detected; Claude Code needs Node 18+."
        fi
    fi
    npm install -g @anthropic-ai/claude-code
    ok "Claude Code CLI installed: $(claude --version 2>/dev/null || echo 'run: claude --version')"
}

write_deepseek_env() {
    local token="$API_KEY"
    local placeholder=0
    if [[ -z "$token" ]]; then
        token="<your DeepSeek API Key>"
        placeholder=1
    fi

    mkdir -p "$(dirname "$ENV_FILE")"
    umask 077
    cat > "$ENV_FILE" <<EOF
# DeepSeek -> Claude Code (Anthropic API) migration.
# Source this file before running 'claude':  source "$ENV_FILE"
# Docs: https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code
#
# Model mapping used by Claude Code:
#   opus  -> $DEEPSEEK_MODEL          (orchestrator / main session, 1M context)
#   sonnet-> $DEEPSEEK_MODEL
#   haiku -> $DEEPSEEK_FAST_MODEL     (cheap/fast)
#   subagents -> $DEEPSEEK_FAST_MODEL (auto-pr-* workers)
export ANTHROPIC_BASE_URL="$DEEPSEEK_BASE_URL"
export ANTHROPIC_AUTH_TOKEN="$token"
export ANTHROPIC_MODEL="$DEEPSEEK_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$DEEPSEEK_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$DEEPSEEK_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$DEEPSEEK_FAST_MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$DEEPSEEK_FAST_MODEL"
export CLAUDE_CODE_EFFORT_LEVEL="max"
EOF
    chmod 600 "$ENV_FILE"
    ok "wrote DeepSeek env file: $ENV_FILE"
    if (( placeholder )); then
        warn "No API key provided. Edit $ENV_FILE and replace '<your DeepSeek API Key>' (or re-run with --api-key)."
    fi
}

detect_shell_rc() {
    if [[ -n "$SHELL_RC" ]]; then
        echo "$SHELL_RC"
        return 0
    fi
    case "$(basename "${SHELL:-bash}")" in
        zsh) echo "$HOME/.zshrc" ;;
        *)   echo "$HOME/.bashrc" ;;
    esac
}

persist_env_source() {
    local rc marker
    rc="$(detect_shell_rc)"
    marker="# auto-pr-skill: DeepSeek env for Claude Code"
    touch "$rc"
    if grep -Fq "$marker" "$rc"; then
        ok "shell rc already sources the DeepSeek env ($rc)"
        return 0
    fi
    {
        printf '\n%s\n' "$marker"
        printf '[ -f "%s" ] && source "%s"\n' "$ENV_FILE" "$ENV_FILE"
    } >> "$rc"
    ok "appended DeepSeek env source to $rc (open a new shell or 'source $rc')"
}

uninstall_deepseek_env() {
    if [[ -f "$ENV_FILE" ]]; then
        rm -f "$ENV_FILE"
        ok "removed $ENV_FILE"
    fi
    local rc marker
    rc="$(detect_shell_rc)"
    marker="# auto-pr-skill: DeepSeek env for Claude Code"
    if [[ -f "$rc" ]] && grep -Fq "$marker" "$rc"; then
        local tmp
        tmp="$(mktemp)"
        grep -vF "$marker" "$rc" | grep -vF "source \"$ENV_FILE\"" > "$tmp" || true
        mv "$tmp" "$rc"
        ok "removed DeepSeek env source from $rc"
    fi
}

# ---------------------------------------------------------------------------
# Skill install / uninstall (shared profiles into ~/.config/auto-pr)
# ---------------------------------------------------------------------------
install_shared() {
    mkdir -p "$GLOBAL_PROFILE_DIR/projects"
    for f in "$SCRIPT_DIR"/projects/*.yaml; do
        [[ -e "$f" ]] || continue
        link "$f" "$GLOBAL_PROFILE_DIR/projects/$(basename "$f")"
    done
    link "$SCRIPT_DIR/lib"        "$GLOBAL_PROFILE_DIR/lib"
    link "$SCRIPT_DIR/templates"  "$GLOBAL_PROFILE_DIR/templates"
    link "$SCRIPT_DIR/references" "$GLOBAL_PROFILE_DIR/references"
}

install_global() {
    log "Installing globally into $GLOBAL_CLAUDE_DIR"

    link "$SCRIPT_DIR/claude/SKILL.md" \
         "$GLOBAL_CLAUDE_DIR/skills/auto-pr-skill/SKILL.md"

    link "$SCRIPT_DIR/claude/commands/auto-pr.md" \
         "$GLOBAL_CLAUDE_DIR/commands/auto-pr.md"

    for f in "$SCRIPT_DIR"/claude/agents/*.md; do
        link "$f" "$GLOBAL_CLAUDE_DIR/agents/$(basename "$f")"
    done

    merge_settings "$GLOBAL_CLAUDE_DIR/settings.json"
    install_shared
}

uninstall_global() {
    log "Uninstalling globally from $GLOBAL_CLAUDE_DIR"
    unlink_if_ours "$GLOBAL_CLAUDE_DIR/skills/auto-pr-skill/SKILL.md" "$SCRIPT_DIR/"
    unlink_if_ours "$GLOBAL_CLAUDE_DIR/commands/auto-pr.md" "$SCRIPT_DIR/"

    if [[ -d "$GLOBAL_CLAUDE_DIR/agents" ]]; then
        for f in "$GLOBAL_CLAUDE_DIR/agents"/auto-pr-*.md; do
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
    unlink_if_ours "$GLOBAL_PROFILE_DIR/lib"        "$SCRIPT_DIR/"
    unlink_if_ours "$GLOBAL_PROFILE_DIR/templates"  "$SCRIPT_DIR/"
    unlink_if_ours "$GLOBAL_PROFILE_DIR/references" "$SCRIPT_DIR/"
}

# Pick the profile yaml that matches the given project directory.
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

    local proj_cl="$proj/.claude"
    local proj_ap="$proj/.auto-pr"

    ensure_local_artifact_ignores "$proj"

    link "$SCRIPT_DIR/claude/SKILL.md" \
         "$proj_cl/skills/auto-pr-skill/SKILL.md"

    for f in "$SCRIPT_DIR"/claude/agents/*.md; do
        link "$f" "$proj_cl/agents/$(basename "$f")"
    done
    link "$SCRIPT_DIR/claude/commands/auto-pr.md" \
         "$proj_cl/commands/auto-pr.md"

    merge_settings "$proj_cl/settings.json"

    local profile
    if profile="$(pick_profile_for_project "$proj")"; then
        link "$profile" "$proj_ap/profile.yaml"
    else
        warn "No matching profile in projects/*.yaml for $(basename "$proj"). Create one and re-run, or place it manually at $proj_ap/profile.yaml."
    fi

    link "$SCRIPT_DIR/references" "$proj_ap/references"
}

uninstall_project() {
    local proj="$1"
    log "Uninstalling from project $proj"
    if [[ -d "$proj/.claude/agents" ]]; then
        for f in "$proj/.claude/agents"/auto-pr-*.md; do
            [[ -e "$f" || -L "$f" ]] || continue
            unlink_if_ours "$f" "$SCRIPT_DIR/"
        done
    fi
    unlink_if_ours "$proj/.claude/skills/auto-pr-skill/SKILL.md" "$SCRIPT_DIR/"
    unlink_if_ours "$proj/.claude/commands/auto-pr.md" "$SCRIPT_DIR/"
    unlink_if_ours "$proj/.auto-pr/profile.yaml"       "$SCRIPT_DIR/"
    unlink_if_ours "$proj/.auto-pr/references"         "$SCRIPT_DIR/"
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
    (( DEEPSEEK )) && uninstall_deepseek_env
    ok "Done."
    exit 0
fi

(( INSTALL_CLI )) && install_cli

if (( ! PROJECT_ONLY )); then
    install_global
fi
[[ -n "$PROJECT_PATH" ]] && install_project "$PROJECT_PATH"

if (( DEEPSEEK )); then
    write_deepseek_env
    (( PERSIST )) && persist_env_source
fi

cat <<EOF

${GREEN}auto-pr-skill installed for Claude Code.${RESET}

EOF

if (( DEEPSEEK )); then
cat <<EOF
${BOLD}DeepSeek migration:${RESET} point Claude Code at DeepSeek models with:

    ${BOLD}source "$ENV_FILE"${RESET}
$( (( PERSIST )) && printf '    (already added to your shell rc; open a new shell to apply)\n' )
EOF
fi

cat <<EOF
Then, from inside a repo with a profile installed:

    ${BOLD}claude${RESET}                 # then type:
    ${BOLD}/auto-pr <project-name>${RESET}

Or per-project:

    cd ${PROJECT_PATH:-/path/to/repo}
    claude
    /auto-pr $(basename "${PROJECT_PATH:-paddle}")

Skill files:      ${BOLD}$GLOBAL_CLAUDE_DIR/{skills,commands,agents}/${RESET}
Profiles:         ${BOLD}$SCRIPT_DIR/projects/*.yaml${RESET}
Add a new project: copy paddle.yaml, edit name/repo_path/build_cmd, re-run install.

EOF
