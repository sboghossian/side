#!/bin/sh
# Side installer · https://github.com/sboghossian/side
# usage: curl -fsSL sboghossian.github.io/side/install.sh | sh
set -e

BASE="${SIDE_BASE:-https://sboghossian.github.io/side}"
SIDE_HOME="${SIDE_HOME:-$HOME/.side}"
BIN_DIR="${SIDE_BIN:-$HOME/.local/bin}"

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo "side: need shasum or sha256sum to verify the download - aborting" >&2
    exit 1
  fi
}

# verify FILE MANIFEST_KEY — checks FILE's sha256 against the hash recorded
# for MANIFEST_KEY (e.g. "app/index.html") in $TMP/manifest.sha256
verify() {
  want="$(awk -v f="$2" '$2==f{print $1}' "$TMP/manifest.sha256")"
  if [ -z "$want" ]; then
    echo "side: $2 is missing from manifest.sha256 - aborting" >&2
    exit 1
  fi
  got="$(sha256_of "$1")"
  if [ "$got" != "$want" ]; then
    echo "side: checksum mismatch for $2 (got $got, expected $want) - aborting" >&2
    exit 1
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "-> detecting platform... $(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
echo "-> installing Side"

curl -fsSL "$BASE/manifest.sha256" -o "$TMP/manifest.sha256"
curl -fsSL "$BASE/app/index.html" -o "$TMP/index.html"
curl -fsSL "$BASE/bin/side-serve.py" -o "$TMP/side-serve.py"
curl -fsSL "$BASE/bin/side" -o "$TMP/side"

echo "-> verifying checksums"
verify "$TMP/index.html" "app/index.html"
verify "$TMP/side-serve.py" "bin/side-serve.py"
verify "$TMP/side" "bin/side"

mkdir -p "$SIDE_HOME/app" "$SIDE_HOME/bin" "$BIN_DIR"
mv "$TMP/index.html" "$SIDE_HOME/app/index.html"
mv "$TMP/side-serve.py" "$SIDE_HOME/bin/side-serve.py"
mv "$TMP/side" "$BIN_DIR/side"
chmod +x "$BIN_DIR/side"
rm -rf "$TMP"

# make sure `side` is on PATH
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    PROFILE=""
    case "${SHELL:-}" in
      */zsh)  PROFILE="$HOME/.zshrc" ;;
      */bash) PROFILE="$HOME/.bashrc" ;;
      *)      PROFILE="$HOME/.profile" ;;
    esac
    if [ -n "$PROFILE" ] && ! grep -qs '\.local/bin' "$PROFILE" 2>/dev/null; then
      printf '\n# Side\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$PROFILE"
      echo "-> added ~/.local/bin to PATH in ${PROFILE##*/} (open a new terminal to pick it up)"
    fi
    ;;
esac

echo "OK installed. run \`side\` to start."
[ -n "${SIDE_NO_LAUNCH:-}" ] && exit 0
exec "$BIN_DIR/side"
