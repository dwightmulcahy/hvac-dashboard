#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  HVAC Dashboard — Backfill GitHub Releases for existing tags
#
#  docker-release.yml (added in a separate change) creates a GitHub
#  Release automatically for every *new* version tag going forward —
#  but it only runs on tag pushes, so tags that already existed
#  before that workflow was added have no Release object behind them.
#  That's why they show up under "Tags" but not "Releases": a Release
#  is a distinct thing from a tag, not implied by one.
#
#  This is a one-time backfill: for every existing vX.Y.Z tag with no
#  Release yet, generates that tag's changelog entry with git-cliff
#  and creates a matching GitHub Release from it. Safe to re-run —
#  tags that already have a Release are skipped, not duplicated.
#
#  Does NOT rebuild or re-push any Docker images — this only creates
#  GitHub Release metadata (title + changelog notes) pointing at the
#  commit each tag already references. The images for old tags are
#  already on Docker Hub from whenever they were originally released;
#  nothing about that changes here.
#
#  Requires: gh (GitHub CLI, authenticated — `gh auth login`),
#  git-cliff (https://git-cliff.org/docs/installation/binary-releases),
#  run from the repo root.
#
#  Usage:
#    ./scripts/backfill-releases.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[0;33m'
RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET} $1"; }
info() { echo -e "${CYAN}→${RESET} $1"; }
warn() { echo -e "${YELLOW}⚠${RESET} $1"; }
err()  { echo -e "${RED}✗${RESET} $1"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}── $1 ──${RESET}"; }

section "Preflight"

command -v gh &>/dev/null || err "GitHub CLI (gh) not found — install it: https://cli.github.com"
command -v git-cliff &>/dev/null || err "git-cliff not found — install it: https://git-cliff.org/docs/installation/binary-releases"
gh auth status &>/dev/null || err "gh is not authenticated — run 'gh auth login' first"
[[ -f cliff.toml ]] || err "cliff.toml not found — run this from the repo root"
ok "gh, git-cliff, and cliff.toml all present"

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
ok "Target repo: $REPO"

# ── generate the full changelog fresh, don't trust a possibly-stale
#    committed CHANGELOG.md ──────────────────────────────────────
section "Generating changelog"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

GIT_CLIFF_REMOTE_GITHUB_OWNER="${REPO%%/*}" \
GIT_CLIFF_REMOTE_GITHUB_REPO="${REPO##*/}" \
  git-cliff > "$TMPDIR/full_changelog.md" 2>/dev/null
ok "Generated $(grep -c '^## \[' "$TMPDIR/full_changelog.md") release section(s)"

# split into one file per version section, keyed by version number
# (without the "v" prefix, matching git-cliff's header format) —
# each section runs from one "## [x.y.z] - date" line up to the next
awk '
  /^## \[/ {
    if (out) close(out)
    ver = $0
    sub(/^## \[/, "", ver)
    sub(/\].*/, "", ver)
    out = "'"$TMPDIR"'/section-" ver ".md"
  }
  out { print > out }
' "$TMPDIR/full_changelog.md"

# ── backfill loop ─────────────────────────────────────────────
section "Creating releases"
CREATED=0
SKIPPED=0
MISSING_SECTION=0
LATEST_TAG=""

for TAG in $(git tag -l --sort=v:refname); do
  if gh release view "$TAG" &>/dev/null; then
    info "$TAG — release already exists, skipping"
    SKIPPED=$((SKIPPED + 1))
    LATEST_TAG="$TAG"
    continue
  fi

  VERSION="${TAG#v}"
  SECTION_FILE="$TMPDIR/section-$VERSION.md"
  if [[ ! -f "$SECTION_FILE" ]]; then
    # every tag should have produced a section above — if one didn't,
    # that's worth surfacing rather than silently creating an empty
    # release, since it likely means the tag doesn't match
    # cliff.toml's tag_pattern or has no conventional commits at all
    warn "$TAG — no changelog section found, skipping (check cliff.toml's tag_pattern)"
    MISSING_SECTION=$((MISSING_SECTION + 1))
    continue
  fi

  TARGET_SHA="$(git rev-list -n1 "$TAG")"
  PRERELEASE_FLAG=()
  if [[ "$TAG" == *-* ]]; then
    PRERELEASE_FLAG=(--prerelease)
  fi

  gh release create "$TAG" \
    --title "$TAG" \
    --notes-file "$SECTION_FILE" \
    --target "$TARGET_SHA" \
    --verify-tag \
    --latest=false \
    "${PRERELEASE_FLAG[@]}" \
    >/dev/null

  ok "$TAG — release created"
  CREATED=$((CREATED + 1))
  LATEST_TAG="$TAG"
done

# ── mark the actual newest tag as "Latest" ────────────────────
section "Marking latest release"
if [[ -n "$LATEST_TAG" ]]; then
  gh release edit "$LATEST_TAG" --latest >/dev/null
  ok "$LATEST_TAG marked as Latest"
else
  warn "No tags processed — nothing to mark as latest"
fi

# ── summary ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Backfill complete${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "  Created  : ${BOLD}$CREATED${RESET}"
echo -e "  Skipped  : ${BOLD}$SKIPPED${RESET} (already had a release)"
if [[ "$MISSING_SECTION" -gt 0 ]]; then
  echo -e "  ${YELLOW}No changelog section: $MISSING_SECTION — see warnings above${RESET}"
fi
echo -e "\n  ${CYAN}$REPO/releases${RESET}"
echo ""
