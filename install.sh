#!/bin/bash
# Install cc-usage to ~/.local/bin
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${CC_USAGE_INSTALL_DIR:-$HOME/.local/bin}"

mkdir -p "$INSTALL_DIR"

# Copy the Python implementation
cp "$SCRIPT_DIR/cc_usage.py" "$INSTALL_DIR/cc_usage.py"
chmod +x "$INSTALL_DIR/cc_usage.py"

# Write the thin wrapper
cat > "$INSTALL_DIR/cc-usage" << EOF
#!/bin/bash
exec python3 "$INSTALL_DIR/cc_usage.py" "\$@"
EOF

chmod +x "$INSTALL_DIR/cc-usage"

echo "Installed cc-usage to $INSTALL_DIR/cc-usage"
echo ""
echo "Make sure $INSTALL_DIR is in your PATH:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
echo ""
echo "Quick test:"
echo "  cc-usage           # token usage for current project"
echo "  cc-usage -v        # verbose (hourly + per-session)"
echo "  cc-usage --json    # machine-readable output"
echo "  cc-usage --install-hook  # add pre-commit hook to current repo"
