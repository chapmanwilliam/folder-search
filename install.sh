#!/bin/sh
# Install or update Folder Search on a Mac.
#
#   curl -LsSf https://raw.githubusercontent.com/chapmanwilliam/folder-search/main/install.sh | sh
#
# What it does: installs uv (a Python package manager) if you do not have
# it, installs Folder Search with it, and puts "Folder Search.app" in
# ~/Applications. Nothing needs sudo. Run it again to update.
set -eu

SOURCE="${FOLDER_SEARCH_SOURCE:-git+https://github.com/chapmanwilliam/folder-search}"
APP="$HOME/Applications/Folder Search.app"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Folder Search currently runs on macOS only." >&2
  exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
  echo "Note: this Mac has an Intel processor. Folder Search will work, but indexing will be much slower than on Apple Silicon."
fi

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH"
  else
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
  fi
fi

echo "Installing Folder Search (the first time downloads about 1 GB of libraries)..."
uv tool install --force --python 3.12 "$SOURCE"
BIN="$(uv tool dir --bin)/folder-search"
PY="$(uv tool dir)/folder-search/bin/python"
ICON="$("$PY" -c 'import folder_search, pathlib; print(pathlib.Path(folder_search.__file__).parent / "assets" / "FolderSearch.icns")')"

# A minimal app bundle, so the app can live in the Dock and be opened from
# Launchpad or Spotlight. It just starts the installed command.
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/MacOS/Folder Search" <<SCRIPT
#!/bin/sh
exec "$BIN" "\$@"
SCRIPT
chmod +x "$APP/Contents/MacOS/Folder Search"
cp "$ICON" "$APP/Contents/Resources/FolderSearch.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Folder Search</string>
  <key>CFBundleDisplayName</key><string>Folder Search</string>
  <key>CFBundleIdentifier</key><string>io.github.chapmanwilliam.folder-search</string>
  <key>CFBundleExecutable</key><string>Folder Search</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>FolderSearch</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Finder caches app icons; touching the bundle makes it look again.
touch "$APP"

echo ""
echo "Done. Open \"Folder Search\" from your Applications folder (in your home folder) or Spotlight."
if ! command -v ocrmypdf >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ocrmypdf ]; then
  echo "Optional: to search scanned PDFs too, install OCR with Homebrew:  brew install ocrmypdf"
fi
