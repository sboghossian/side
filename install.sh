#!/bin/sh
# Side installer · https://github.com/sboghossian/side
# usage: curl -fsSL sboghossian.github.io/side/install.sh | sh
set -e

BASE="${SIDE_BASE:-https://sboghossian.github.io/side}"
SIDE_HOME="${SIDE_HOME:-$HOME/.side}"
BIN_DIR="${SIDE_BIN:-$HOME/.local/bin}"

echo "-> detecting platform... $(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
echo "-> installing Side"

mkdir -p "$SIDE_HOME/app" "$SIDE_HOME/bin" "$BIN_DIR"
curl -fsSL "$BASE/app/index.html" -o "$SIDE_HOME/app/index.html"
curl -fsSL "$BASE/bin/side-serve.py" -o "$SIDE_HOME/bin/side-serve.py"
curl -fsSL "$BASE/bin/side" -o "$BIN_DIR/side"
chmod +x "$BIN_DIR/side"

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
