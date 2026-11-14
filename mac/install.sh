#!/bin/bash
# Jarvis for Mac - one-line installer (safe to run again: it updates in place).
#   curl -fsSL https://raw.githubusercontent.com/mahirb03/Jarvis-updates/main/mac/install.sh | bash
set -e
BASE="https://raw.githubusercontent.com/mahirb03/Jarvis-updates/main"
DATA="$HOME/Library/Application Support/Jarvis"
APPB="$HOME/Applications/Jarvis.app"

say_step() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }

say_step "Setting up folders"
mkdir -p "$DATA/app" "$HOME/Applications"

say_step "Getting Python tools (uv)"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

say_step "Downloading Jarvis"
curl -fsSL "$BASE/launcher.py" -o "$DATA/launcher.py"
curl -fsSL "$BASE/requirements.txt" -o "$DATA/requirements.txt"
curl -fsSL "$BASE/app/updater.py" -o "$DATA/app/updater.py"
[ -f "$DATA/app/version.json" ] || echo '{"version": 0}' > "$DATA/app/version.json"

say_step "Installing Python 3.12 and packages (a few minutes the first time)"
[ -x "$DATA/venv/bin/python" ] || "$UV" venv --python 3.12 "$DATA/venv" >/dev/null
"$UV" pip install --python "$DATA/venv/bin/python" -q -r "$DATA/requirements.txt"

say_step "Fetching the latest app files"
cd "$DATA"
"$DATA/venv/bin/python" -c "import sys; sys.path.insert(0, 'app'); import updater; print(updater.update_app_files('app'))"

say_step "Creating Jarvis.app"
rm -rf "$APPB"
mkdir -p "$APPB/Contents/MacOS" "$APPB/Contents/Resources"
cat > "$APPB/Contents/MacOS/Jarvis" <<'SH'
#!/bin/bash
DATA="$HOME/Library/Application Support/Jarvis"
export JARVIS_DATA="$DATA" JARVIS_INSTALLED=1 JARVIS_APP_BUNDLE="$HOME/Applications/Jarvis.app"
cd "$DATA"
exec "$DATA/venv/bin/python" "$DATA/launcher.py" "$@" >> "$DATA/jarvis.log" 2>&1
SH
chmod +x "$APPB/Contents/MacOS/Jarvis"
cat > "$APPB/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundleIdentifier</key><string>com.mahirb03.jarvis</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundleIconFile</key><string>jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Jarvis listens for "Hey Jarvis" and your voice commands.</string>
  <key>NSAppleEventsUsageDescription</key><string>Jarvis controls apps (like Claude and Music) when you ask it to.</string>
</dict></plist>
PL
if [ -f "$DATA/app/static/logo.png" ]; then
  IS="$(mktemp -d)/jarvis.iconset"; mkdir -p "$IS"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s "$DATA/app/static/logo.png" --out "$IS/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    d=$((s*2)); sips -z $d $d "$DATA/app/static/logo.png" --out "$IS/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$IS" -o "$APPB/Contents/Resources/jarvis.icns" 2>/dev/null || true
fi
codesign --force --deep -s - "$APPB" >/dev/null 2>&1 || true

if [ -n "$CI" ]; then echo "CI: skipping launch"; exit 0; fi

say_step "Starting Jarvis"
open "$APPB"
printf "\n\033[1;32mDone.\033[0m Jarvis is in your Applications folder (and Launchpad).\n"
printf "When macOS asks to use the microphone, click Allow.\n"
