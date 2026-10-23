#!/bin/sh
# Rebuild the app icon from assets/icon.svg and assets/icon-small.svg.
# Needs rsvg-convert (brew install librsvg); iconutil ships with macOS.
#
# The small drawing (bigger lens, no text lines) is used at 16 and 32 px,
# where the full drawing's detail blurs into mush.
set -eu
cd "$(dirname "$0")/.."
SET="$(mktemp -d)/FolderSearch.iconset"
mkdir -p "$SET"
r() { rsvg-convert -w "$2" -h "$2" "$1" -o "$SET/$3"; }
r assets/icon-small.svg 16  icon_16x16.png
r assets/icon-small.svg 32  icon_16x16@2x.png
r assets/icon-small.svg 32  icon_32x32.png
r assets/icon.svg       64  icon_32x32@2x.png
r assets/icon.svg       128 icon_128x128.png
r assets/icon.svg       256 icon_128x128@2x.png
r assets/icon.svg       256 icon_256x256.png
r assets/icon.svg       512 icon_256x256@2x.png
r assets/icon.svg       512 icon_512x512.png
r assets/icon.svg       1024 icon_512x512@2x.png
iconutil -c icns "$SET" -o src/folder_search/assets/FolderSearch.icns
cp assets/icon-small.svg src/folder_search/ui/icon.svg  # 26 px in the sidebar: small drawing
rsvg-convert -w 256 -h 256 assets/icon.svg -o assets/icon-256.png
echo "built src/folder_search/assets/FolderSearch.icns"
