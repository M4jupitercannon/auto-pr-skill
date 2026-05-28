#!/usr/bin/env bash
# init_run.sh — initialize a fresh /auto-pr run directory.
#
# Usage:
#     lib/init_run.sh <project-name>
#
# Resolves the profile, creates <repo_path>/.auto-pr/run-<UTC>/ with an empty
# state.json, and prints the absolute path of the run dir on stdout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ $# -lt 1 ]]; then
    echo "usage: init_run.sh <project-name> [--repo PATH]" >&2
    exit 2
fi

PROJECT_NAME="$1"; shift || true
REPO_OVERRIDE=""
PROJECT_NAME_LC="$(printf '%s' "$PROJECT_NAME" | tr '[:upper:]' '[:lower:]')"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO_OVERRIDE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve profile
# ---------------------------------------------------------------------------
PROFILE=""

profile_name() {
    awk -F': *' '
        $1 == "name" {
            sub(/^[^:]+: */, "")
            gsub(/^"|"$/, "")
            print tolower($0)
            exit
        }
    ' "$1"
}

profile_matches_project() {
    local pname
    pname="$(profile_name "$1")"
    [[ -n "$pname" && "$pname" == "$PROJECT_NAME_LC" ]]
}

find_profile() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1

    local f
    for f in "$dir"/*.yaml "$dir"/*.yml; do
        [[ -e "$f" ]] || continue
        local base pname
        base="$(basename "$f")"
        base="${base%.*}"
        if [[ "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" == "$PROJECT_NAME_LC" ]]; then
            echo "$f"
            return 0
        fi
        pname="$(profile_name "$f")"
        if [[ -n "$pname" && "$pname" == "$PROJECT_NAME_LC" ]]; then
            echo "$f"
            return 0
        fi
    done
    return 1
}

ensure_local_artifact_ignores() {
    local repo="$1"
    local git_dir exclude_file pattern missing=()

    if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    git_dir="$(git -C "$repo" rev-parse --absolute-git-dir)"
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
}

# 1. If the caller passed --repo, prefer <repo>/.auto-pr/profile.yaml
if [[ -n "$REPO_OVERRIDE" && -f "$REPO_OVERRIDE/.auto-pr/profile.yaml" ]] \
    && profile_matches_project "$REPO_OVERRIDE/.auto-pr/profile.yaml"; then
    PROFILE="$REPO_OVERRIDE/.auto-pr/profile.yaml"
fi

# 2. Per-cwd .auto-pr/profile.yaml (if running from inside a project)
if [[ -z "$PROFILE" && -f ".auto-pr/profile.yaml" ]] \
    && profile_matches_project ".auto-pr/profile.yaml"; then
    PROFILE="$(pwd)/.auto-pr/profile.yaml"
fi

# 3. Global profile dir
if [[ -z "$PROFILE" ]]; then
    PROFILE="$(find_profile "${XDG_CONFIG_HOME:-$HOME/.config}/auto-pr/projects" || true)"
fi

# 4. Skill repo's projects/
if [[ -z "$PROFILE" ]]; then
    PROFILE="$(find_profile "$SKILL_ROOT/projects" || true)"
fi

if [[ -z "$PROFILE" ]]; then
    echo "ERROR: no profile found for '$PROJECT_NAME'." >&2
    echo "Looked in: \$repo/.auto-pr/profile.yaml, ~/.config/auto-pr/projects/*.{yaml,yml}, $SKILL_ROOT/projects/*.{yaml,yml}" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Read profile fields without YAML parser (simple key: value lines).
# ---------------------------------------------------------------------------
yaml_get() {
    awk -v key="$1" -F': *' '
        $1 == key { sub(/^[^:]+: */, ""); gsub(/^"|"$/, ""); print; exit }
    ' "$PROFILE"
}

REPO_PATH="${REPO_OVERRIDE:-$(yaml_get repo_path)}"
if [[ -z "$REPO_PATH" ]]; then
    echo "ERROR: profile $PROFILE missing repo_path" >&2
    exit 4
fi
[[ -d "$REPO_PATH" ]] || { echo "ERROR: repo_path does not exist: $REPO_PATH" >&2; exit 5; }

ensure_local_artifact_ignores "$REPO_PATH"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$REPO_PATH/.auto-pr/run-$TS"
mkdir -p "$RUN_DIR/tasks"

# Snapshot the profile inside the run dir for reproducibility.
cp "$PROFILE" "$RUN_DIR/profile.yaml"

cat > "$RUN_DIR/state.json" <<JSON
{
  "schema_version": 1,
  "phase": "init",
  "project": "$PROJECT_NAME",
  "profile_path": "$PROFILE",
  "repo_path": "$REPO_PATH",
  "run_dir": "$RUN_DIR",
  "started_at": "$TS",
  "tasks_total": 0,
  "tasks_done": 0,
  "tasks_abandoned": 0,
  "tasks_stuck": 0,
  "human_review_needed": 0,
  "current_task": null,
  "prs": [],
  "skips": []
}
JSON

# Convenience symlink: <repo>/.auto-pr/latest -> run-<TS>/
ln -sfn "$RUN_DIR" "$REPO_PATH/.auto-pr/latest"

echo "$RUN_DIR"
