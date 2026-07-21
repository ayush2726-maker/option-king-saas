#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== OKAI LOCAL GATEWAY TERMUX INSTALL ==="
pkg update -y
pkg install -y python git clang libxml2 libxslt
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "$ROOT_DIR/requirements.txt"

mkdir -p "$HOME/.termux/boot" "$HOME/.okai/logs"
cat > "$HOME/.okai/start_gateway.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$ROOT_DIR"
termux-wake-lock 2>/dev/null || true
while true; do
  python -u okai_local_gateway.py run >> "$HOME/.okai/logs/gateway.log" 2>&1
  sleep 5
done
EOF
chmod 700 "$HOME/.okai/start_gateway.sh"

cat > "$HOME/.termux/boot/start-okai-gateway" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
nohup "$HOME/.okai/start_gateway.sh" >/dev/null 2>&1 &
EOF
chmod 700 "$HOME/.termux/boot/start-okai-gateway"
chmod 700 "$ROOT_DIR/okai_local_gateway.py"

echo "✅ Dependencies and auto-restart script installed"
echo "Run next: python $ROOT_DIR/okai_local_gateway.py setup"
echo "For phone reboot auto-start, install/open Termux:Boot once."
