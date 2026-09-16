#!/usr/bin/env bash
set -euo pipefail

# Installs pinned, checksum-verified scanner binaries into $1 (default
# ~/.local/bin) for linux/amd64 (GitHub-hosted ubuntu runners).
# Bump a version = update BOTH the version and its sha256 below.

DEST="${1:-$HOME/.local/bin}"
mkdir -p "${DEST}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

GITLEAKS_VERSION=8.30.1
GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
OSV_VERSION=2.6.0
OSV_SHA256=ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108
OPENGREP_VERSION=1.30.0
OPENGREP_SHA256=35779bdd72e92129c8df2a77f0c55e8c08356801ea92591ef32108d6b28d564c
TRIVY_VERSION=0.74.0
TRIVY_SHA256=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
ACTIONLINT_VERSION=1.7.12
ACTIONLINT_SHA256=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
ZIZMOR_VERSION=1.30.1
# opengrep-rules has no releases; pin a commit.
OPENGREP_RULES_COMMIT=f1d2b562b414783763fd02a6ed2736eaed622efa

fetch() {  # url sha256 out
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet -
}

fetch "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
  "${GITLEAKS_SHA256}" "${TMP}/gitleaks.tgz"
tar -xzf "${TMP}/gitleaks.tgz" -C "${DEST}" gitleaks

fetch "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" \
  "${OSV_SHA256}" "${DEST}/osv-scanner"

fetch "https://github.com/opengrep/opengrep/releases/download/v${OPENGREP_VERSION}/opengrep_manylinux_x86" \
  "${OPENGREP_SHA256}" "${DEST}/opengrep"

fetch "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
  "${TRIVY_SHA256}" "${TMP}/trivy.tgz"
tar -xzf "${TMP}/trivy.tgz" -C "${DEST}" trivy

fetch "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" \
  "${ACTIONLINT_SHA256}" "${TMP}/actionlint.tgz"
tar -xzf "${TMP}/actionlint.tgz" -C "${DEST}" actionlint

chmod +x "${DEST}"/{gitleaks,osv-scanner,opengrep,trivy,actionlint}

# ubuntu-latest's system Python is externally managed (PEP 668): prefer pipx.
if command -v pipx > /dev/null; then
  pipx install --force "zizmor==${ZIZMOR_VERSION}" > /dev/null
else
  python3 -m pip install --quiet --user "zizmor==${ZIZMOR_VERSION}"
fi

RULES_DIR="${OPENGREP_RULES_DIR:-$HOME/.cache/opengrep-rules}"
if [ ! -d "${RULES_DIR}/.git" ]; then
  git init -q "${RULES_DIR}"
  git -C "${RULES_DIR}" remote add origin https://github.com/opengrep/opengrep-rules.git
fi
git -C "${RULES_DIR}" fetch -q --depth 1 origin "${OPENGREP_RULES_COMMIT}"
git -C "${RULES_DIR}" checkout -q FETCH_HEAD
echo "Installed scanners into ${DEST}; opengrep rules at ${RULES_DIR}"
