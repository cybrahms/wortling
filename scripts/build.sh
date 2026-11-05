#!/usr/bin/env bash
# build.sh — build the wortling image from a commit that main contains.
#
# Compose builds from whatever checkout is present (`build: .`), so before this
# gate existed a branch that had never reached the forge could become the
# running service. This fetches main, refuses when HEAD is not on it, and only
# then builds, labelling the image with the commit it came from.
#
#   scripts/build.sh              check, then build
#   scripts/build.sh --check-only check and stop (used by the gate's tests)
#   scripts/build.sh --repo DIR   check a different checkout (tests only)
#
# WORTLING_IMAGE overrides the image tag (default wortling:trial).
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --repo) REPO=$2; shift 2 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "build: unknown argument $1" >&2; exit 64 ;;
  esac
done

IMAGE=${WORTLING_IMAGE:-wortling:trial}
BASE=main

cd "$REPO"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "build: $REPO is not a git checkout, so there is no commit to build" >&2
  exit 1
fi

# The label is a claim about a commit, so the tree has to be that commit.
if [ -n "$(git status --porcelain)" ]; then
  echo "build: the checkout is dirty, so the image would not be commit $(git rev-parse --short HEAD); commit or stash first" >&2
  exit 1
fi

# Fail closed: an unreachable forge must not become a reason to skip the check.
remote=origin
git remote get-url "$remote" >/dev/null 2>&1 || { remote=gitea; }
if ! git remote get-url "$remote" >/dev/null 2>&1; then
  echo "build: no $BASE remote (origin or gitea) in $REPO; refusing to build without knowing what $BASE contains" >&2
  exit 1
fi
if ! git fetch --no-tags --quiet "$remote" "+refs/heads/$BASE:refs/remotes/$remote/$BASE"; then
  echo "build: could not fetch $BASE from $remote; refusing to build without knowing what $BASE contains" >&2
  exit 1
fi

sha=$(git rev-parse HEAD)
main_sha=$(git rev-parse "refs/remotes/$remote/$BASE")
# main must contain this commit: a branch head that was never merged is not a
# deployable build, which is the incident this gate exists for (2026-10-26).
if ! git merge-base --is-ancestor "$sha" "$main_sha"; then
  echo "build: refusing to build $sha: $BASE at $main_sha does not contain it" >&2
  echo "build: commits this build has that $BASE does not:" >&2
  git log --oneline "$main_sha..$sha" | sed 's/^/  /' >&2
  echo "build: $BASE commits this build lacks: $(git rev-list --count "$sha..$main_sha")" >&2
  exit 1
fi
behind=$(git rev-list --count "$sha..$main_sha")
if [ "$behind" != 0 ]; then
  echo "build: $sha is on $BASE but $behind commit(s) behind it"
fi
echo "build: $BASE at $main_sha contains $sha"

if [ "$CHECK_ONLY" = 1 ]; then
  echo "build: check only, not building"
  exit 0
fi

if command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
elif docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
else
  echo "build: neither docker-compose nor the docker compose plugin is installed" >&2
  exit 1
fi

WORTLING_IMAGE=$IMAGE BUILD_SHA=$sha "${compose[@]}" build --build-arg BUILD_SHA="$sha" api

label=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE")
if [ "$label" != "$sha" ]; then
  echo "build: $IMAGE carries revision '$label', expected $sha" >&2
  exit 1
fi
echo "build: $IMAGE labelled with $sha"
