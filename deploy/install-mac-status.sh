#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# != 0 && $# != 3 ]]; then
  echo "Usage: $0 [SSH_HOST GATEWAY_HTTPS_URL DASHBOARD_HTTPS_URL]" >&2
  exit 2
fi
if [[ -z "${STATUS_DEVELOPMENT_TEAM:-}" ]]; then
  echo "Set STATUS_DEVELOPMENT_TEAM to the team ID of your Apple Development certificate." >&2
  exit 2
fi
if ! command -v xcodegen >/dev/null; then
  echo "XcodeGen is required. Install it with: brew install xcodegen" >&2
  exit 2
fi

derived="$root/build/DerivedData-mac"
xcodegen generate --spec "$root/mac-status/project.yml"
xcodebuild -quiet -project "$root/mac-status/HomeServicesStatus.xcodeproj" \
  -scheme HomeServicesStatus -configuration Release -destination "platform=macOS,arch=$(uname -m)" \
  -derivedDataPath "$derived" DEVELOPMENT_TEAM="$STATUS_DEVELOPMENT_TEAM" \
  CODE_SIGN_IDENTITY='Apple Development' build

name='Home Services Status'
applications="$HOME/Applications"
app="$applications/$name.app"
binary="$app/Contents/MacOS/$name"
label='com.zacharysturman.homeservices.status.collector'
agents="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"
mkdir -p "$applications" "$agents"
stage="$(mktemp -d "$applications/.HomeServicesStatus.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
/usr/bin/ditto "$derived/Build/Products/Release/$name.app" "$stage/$name.app"
/usr/bin/codesign --verify --deep --strict "$stage/$name.app"

# Write the plist with plutil so spaces and XML special characters in the
# user's home directory are preserved.
plist="$stage/$label.plist"
/usr/bin/plutil -create xml1 "$plist"
/usr/bin/plutil -insert Label -string "$label" "$plist"
/usr/bin/plutil -insert ProgramArguments -json '[]' "$plist"
/usr/bin/plutil -insert ProgramArguments.0 -string "$binary" "$plist"
/usr/bin/plutil -insert ProgramArguments.1 -string '--collect' -- "$plist"
/usr/bin/plutil -insert RunAtLoad -bool YES "$plist"
/usr/bin/plutil -insert StartInterval -integer 300 "$plist"

if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then
  /bin/launchctl bootout "$domain/$label"
fi
if [[ -d "$app" ]]; then mv "$app" "$stage/previous.app"; fi
if ! mv "$stage/$name.app" "$app"; then
  if [[ -d "$stage/previous.app" ]]; then mv "$stage/previous.app" "$app"; fi
  exit 1
fi
if [[ $# == 3 ]]; then "$binary" --configure "$@"; fi
mv "$plist" "$agents/$label.plist"
/bin/launchctl bootstrap "$domain" "$agents/$label.plist"
/usr/bin/pluginkit -a "$app/Contents/PlugIns/HomeServicesWidget.appex"
/usr/bin/open "$app"
echo "Installed $name. Control-click the desktop, choose Edit Widgets, and search for $name."
