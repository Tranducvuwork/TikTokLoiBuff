#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="AutoLikeCmt"
APP_BUNDLE="$APP_NAME.app"
DMG_NAME="$APP_NAME.dmg"
DMG_VOLUME="$APP_NAME"
BUILD_DIR="$SCRIPT_DIR/build"
DMG_DIR="$BUILD_DIR/dmg"
APP_DIR="$DMG_DIR/$APP_BUNDLE"
RESOURCES="$APP_DIR/Contents/Resources"
MACOS="$APP_DIR/Contents/MacOS"

# Source: the data directory with runtime, profile, etc.
SOURCE_DIR="$SCRIPT_DIR/AutoLikeCmt-v90-1000Live-a1b5068ae139-data"

echo "======================================"
echo "  Building $APP_NAME.dmg"
echo "======================================"

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$RESOURCES" "$MACOS"

# --- Info.plist ---
cat > "$APP_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>AutoLikeCmt</string>
    <key>CFBundleDisplayName</key>
    <string>Auto Like Cmt</string>
    <key>CFBundleIdentifier</key>
    <string>com.autolikecmt.app</string>
    <key>CFBundleVersion</key>
    <string>95</string>
    <key>CFBundleShortVersionString</key>
    <string>0.95</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
</dict>
</plist>
PLIST

# --- Launcher script (runs in Terminal, handles setup + run in one flow) ---
cat > "$MACOS/launcher" << 'LAUNCHER'
#!/bin/bash

APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$APP_CONTENTS/Resources"
INSTALL_DIR="$HOME/AutoLikeCmt"

# If not running inside Terminal, relaunch in Terminal so user can see output
if [[ -z "$TERM_PROGRAM" && -z "$__LAUNCHED_FROM_TERMINAL" ]]; then
    export __LAUNCHED_FROM_TERMINAL=1
    osascript -e "
        tell application \"Terminal\"
            activate
            do script \"export __LAUNCHED_FROM_TERMINAL=1; bash '$APP_CONTENTS/MacOS/launcher'\"
        end tell
    "
    exit 0
fi

echo "======================================"
echo "  Auto-Like-Cmt"
echo "======================================"
echo ""

# Copy resources if not installed yet
if [[ ! -d "$INSTALL_DIR/runtime" ]]; then
    echo ">> Lan dau su dung - dang copy file..."
    mkdir -p "$INSTALL_DIR"
    rsync -a "$RESOURCES/" "$INSTALL_DIR/"
    echo ">> Copy xong."
    echo ""
fi

# Setup if venv doesn't exist
if [[ ! -d "$INSTALL_DIR/.venv" ]] || ! "$INSTALL_DIR/.venv/bin/python3" -c "import sys" 2>/dev/null; then
    echo ">> Can cai dat moi truong. Bat dau setup..."
    echo ""
    cd "$INSTALL_DIR"
    bash "$INSTALL_DIR/setup.command"
    echo ""
fi

# Verify venv works
if ! "$INSTALL_DIR/.venv/bin/python3" -c "import sys" 2>/dev/null; then
    echo ""
    echo "LOI: Cai dat khong thanh cong!"
    echo "Thu xoa folder va cai lai:"
    echo "  rm -rf ~/AutoLikeCmt"
    echo "  Mo lai app"
    echo ""
    read -p "Nhan Enter de dong..."
    exit 1
fi

# Run the app
echo ">> Dang khoi dong Auto-Like-Cmt..."
cd "$INSTALL_DIR"
"$INSTALL_DIR/.venv/bin/python3" "$INSTALL_DIR/run_macos.py"

echo ""
echo "[App da dong]"
read -p "Nhan Enter de dong Terminal..."
LAUNCHER
chmod +x "$MACOS/launcher"

# --- Copy resources ---
echo ">> Copying runtime..."
cp -R "$SOURCE_DIR/runtime" "$RESOURCES/"

echo ">> Copying profile..."
cp -R "$SOURCE_DIR/profile" "$RESOURCES/"

echo ">> Copying scripts..."
cp "$SOURCE_DIR/run_macos.py" "$RESOURCES/"
cp "$SOURCE_DIR/run_macos.py" "$RESOURCES/"

# Copy requirements.txt and HUONG-DAN.txt
cp "$SCRIPT_DIR/requirements.txt" "$RESOURCES/"
cp "$SCRIPT_DIR/HUONG-DAN.txt" "$RESOURCES/"

# --- Setup.command inside Resources (for first-run install) ---
cat > "$RESOURCES/setup.command" << 'SETUP'
#!/bin/bash
set -e

cd "$(dirname "$0")"
APP_DIR="$(pwd)"

echo "======================================"
echo "  Auto-Like-Cmt - Cai dat lan dau"
echo "======================================"
echo ""

if [[ "$(uname)" != "Darwin" ]]; then
    echo "LOI: Chi chay tren macOS!"
    exit 1
fi

# Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo ">> Cai dat Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

# Check/install Python (prefer Homebrew over uv/system — uv Python breaks venv)
PYTHON=""

# 1. Check Homebrew Python first
if [[ -f /opt/homebrew/bin/python3.14 ]]; then
    PYTHON="/opt/homebrew/bin/python3.14"
    echo ">> Homebrew Python 3.14: $PYTHON"
elif [[ -f /usr/local/bin/python3.14 ]]; then
    PYTHON="/usr/local/bin/python3.14"
    echo ">> Homebrew Python 3.14: $PYTHON"
elif [[ -f /opt/homebrew/bin/python3 ]]; then
    PY_VER="$(/opt/homebrew/bin/python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')"
    PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"
    if [[ "$PY_MINOR" -ge 12 ]]; then
        PYTHON="/opt/homebrew/bin/python3"
        echo ">> Homebrew Python: $PYTHON"
    fi
fi

# 2. Test if found Python can create venv (skip broken uv installs)
if [[ -z "$PYTHON" ]]; then
    for P in $(which -a python3.14 python3.13 python3.12 python3 2>/dev/null | sort -u); do
        PV="$($P --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')"
        PM="$(echo "$PV" | cut -d. -f2)"
        if [[ "$(echo "$PV" | cut -d. -f1)" -ge 3 && "$PM" -ge 12 ]]; then
            # Quick test: can this Python create a venv?
            TEST_DIR=$(mktemp -d)
            if "$P" -m venv "$TEST_DIR/test_venv" 2>/dev/null; then
                rm -rf "$TEST_DIR"
                PYTHON="$P"
                echo ">> Python: $PYTHON"
                break
            fi
            rm -rf "$TEST_DIR"
        fi
    done
fi

# 3. Install via Homebrew if nothing works
if [[ -z "$PYTHON" ]]; then
    echo ">> Cai dat Python 3.14 qua Homebrew..."
    brew install python@3.14
    if [[ -f /opt/homebrew/bin/python3.14 ]]; then
        PYTHON="/opt/homebrew/bin/python3.14"
    elif [[ -f /usr/local/bin/python3.14 ]]; then
        PYTHON="/usr/local/bin/python3.14"
    else
        PYTHON="$(brew --prefix python@3.14)/bin/python3.14"
    fi
fi

echo ">> Python: $PYTHON ($($PYTHON --version))"

# Install tkinter (required by app)
PY_SHORT="$($PYTHON --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')"
if ! "$PYTHON" -c "import tkinter" 2>/dev/null; then
    echo ">> Cai dat tkinter..."
    brew install python-tk@"$PY_SHORT" 2>/dev/null || brew install python-tk 2>/dev/null || true
fi

# Create venv (try with pip first, fallback to without-pip + get-pip.py)
if [[ ! -d "$APP_DIR/.venv" ]]; then
    echo ">> Tao moi truong ao (venv)..."
    if ! "$PYTHON" -m venv "$APP_DIR/.venv" 2>/dev/null; then
        echo ">> Tao venv khong pip (Python managed by uv/system)..."
        "$PYTHON" -m venv --without-pip "$APP_DIR/.venv"
        echo ">> Cai dat pip bang get-pip.py..."
        curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        "$APP_DIR/.venv/bin/python3" /tmp/get-pip.py -q
        rm -f /tmp/get-pip.py
    fi
fi

echo ">> Cai dat thu vien..."
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# Create data directory
DATA_DIR="$HOME/.auto-like-cmt"
mkdir -p "$DATA_DIR"

if [[ ! -f "$DATA_DIR/local_pool_data.json" ]]; then
    echo '{"accounts":[]}' > "$DATA_DIR/local_pool_data.json"
    echo ">> Da tao file pool rong tai: $DATA_DIR/local_pool_data.json"
fi

# Check Chrome
if [[ -d "/Applications/Google Chrome.app" ]]; then
    echo ">> Google Chrome: OK"
else
    echo ""
    echo "CANH BAO: Chua cai Google Chrome!"
    echo "Tai ve tai: https://www.google.com/chrome/"
fi

echo ""
echo "======================================"
echo "  CAI DAT THANH CONG!"
echo "======================================"
echo ""
echo "Cai dat xong! Dang khoi dong app..."
echo ""
SETUP
chmod +x "$RESOURCES/setup.command"

# --- Create DMG ---
echo ""
echo ">> Tao DMG..."

# Create Applications symlink
ln -s /Applications "$DMG_DIR/Applications"

# Detach any existing AutoLikeCmt volumes
hdiutil detach "/Volumes/$DMG_VOLUME" 2>/dev/null || true

# Remove old DMG
rm -f "$SCRIPT_DIR/$DMG_NAME"

# Create temporary read-write DMG
TEMP_DMG="$BUILD_DIR/temp.dmg"
hdiutil create -volname "$DMG_VOLUME" -srcfolder "$DMG_DIR" -ov -format UDRW "$TEMP_DMG"

# Mount it to customize
MOUNT_POINT=$(hdiutil attach "$TEMP_DMG" 2>/dev/null | grep "/Volumes/" | sed 's/.*\(\/Volumes\/.*\)/\1/' | head -1)

echo ">> Tuy chinh giao dien DMG..."

# Set DMG window appearance using AppleScript
osascript << EOF
tell application "Finder"
    tell disk "$DMG_VOLUME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {200, 120, 800, 440}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 100
        -- Position the app on the left and Applications on the right
        set position of item "$APP_BUNDLE" of container window to {140, 160}
        set position of item "Applications" of container window to {460, 160}
        close
        open
        update without registering applications
        delay 1
        close
    end tell
end tell
EOF

# Unmount
hdiutil detach "$MOUNT_POINT" 2>/dev/null

# Convert to compressed read-only DMG
hdiutil convert "$TEMP_DMG" -format UDZO -o "$SCRIPT_DIR/$DMG_NAME"

# Clean up
rm -rf "$BUILD_DIR"

echo ""
echo "======================================"
echo "  HOAN THANH!"
echo "======================================"
echo "  DMG: $SCRIPT_DIR/$DMG_NAME"
echo "  Size: $(du -h "$SCRIPT_DIR/$DMG_NAME" | awk '{print $1}')"
echo ""
