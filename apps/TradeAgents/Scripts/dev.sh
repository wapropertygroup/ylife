#!/usr/bin/env bash
#
# Build, relaunch and screenshot the macOS build of TradeAgents.
#
# This exists because macOS is the *only* platform this app can currently be run and
# looked at on. See apps/README.md: there is no iOS simulator runtime installed and no
# code-signing identity, so the iOS target compiles but cannot be launched. Every
# visual claim about this app therefore has to be made against the Mac build.
#
# Usage:
#   Scripts/dev.sh build          # build only
#   Scripts/dev.sh run            # build, then relaunch
#   Scripts/dev.sh shot [out.png] # build, relaunch, screenshot the window
#   Scripts/dev.sh ios            # compile-check the iOS target (device SDK)
#
set -euo pipefail

cd "$(dirname "$0")/.."

APP="build/Debug/TradeAgents.app"
BIN="$APP/Contents/MacOS/TradeAgents"
OUT="${2:-/tmp/tradeagents.png}"


build() {
	# -quiet still prints errors and warnings, just not the full compiler invocation,
	# which is several thousand characters per file and drowns the diagnostics.
	#
	# -destination is deliberately not used: it is ignored without a scheme
	# ("Ignoring provided run destination because no scheme was passed"), and this
	# project has no shared scheme. Pinning ARCHS achieves the same thing — one slice
	# instead of arm64 plus x86_64, which is twice the work for a binary only ever run
	# on this machine.
	#
	# `set -e` makes a failure here abort the script, which matters: `run` and `shot`
	# would otherwise carry on and relaunch or photograph the *previous* build, and a
	# screenshot of stale code is worse than no screenshot.
	xcodebuild -project TradeAgents.xcodeproj \
		-target TradeAgents -configuration Debug -quiet \
		-sdk macosx ARCHS=arm64 ONLY_ACTIVE_ARCH=NO build
}

# Launch through `open`.
#
# Required rather than executing the inner binary: run directly, a sandboxed app starts
# without its container and never creates a window at all — it just sits there
# frontmost and empty.
relaunch() {
	# Matched on the bundle-relative suffix, not on "$PWD/$BIN". An absolute-path
	# pattern misses an instance that was started with a relative path, and the
	# consequence is not a stale process quietly lingering: `open` finds the old
	# instance already registered and simply activates it, so the build that was just
	# compiled never runs and the screenshot shows the previous one.
	pkill -f "TradeAgents.app/Contents/MacOS/TradeAgents" 2>/dev/null || true
	sleep 1
	open "$APP"
}

# Capture only the app's own window, after moving it somewhere capturable.
#
# `screencapture -R` takes a rect in the *main* display's coordinate space. This machine
# has three displays, and SwiftUI is perfectly happy to open the window on a secondary
# one at a global x of 2210 — at which point the rect read from the accessibility API
# refers to pixels `-R` cannot reach, and the capture silently returns whatever is on
# the main display instead. That produced screenshots of a browser and of a mail client
# while every check still said "the window is there and the app is frontmost".
#
# So the window is *moved* onto the main display, the move is read back, and anything
# unexpected refuses. A missing screenshot is obvious; a wrong one is not.
RECT_X=60
RECT_Y=60
RECT_W=1280
RECT_H=900

shot() {
	osascript -e 'tell application "TradeAgents" to activate' >/dev/null 2>&1 || true
	sleep 1

	osascript <<-APPLESCRIPT >/dev/null 2>&1 || true
		tell application "System Events"
			tell process "TradeAgents"
				set position of window 1 to {$RECT_X, $RECT_Y}
				set size of window 1 to {$RECT_W, $RECT_H}
			end tell
		end tell
	APPLESCRIPT
	sleep 2

	local geom expected front
	expected="$RECT_X,$RECT_Y,$RECT_W,$RECT_H"
	if ! geom=$(osascript -e \
		'tell application "System Events" to tell process "TradeAgents" to get {position, size} of window 1' \
		2>&1); then
		echo "Refusing to capture: no TradeAgents window (${geom})." >&2
		echo "The app may have failed to launch, or Accessibility permission is not" >&2
		echo "granted to this terminal (System Settings > Privacy & Security)." >&2
		return 1
	fi

	geom=$(echo "$geom" | tr -d ' ')
	if [ "$geom" != "$expected" ]; then
		echo "Refusing to capture: window is at '$geom', expected '$expected'." >&2
		echo "It may be on a secondary display, which -R cannot address." >&2
		return 1
	fi

	front=$(osascript -e \
		'tell application "System Events" to get name of first process whose frontmost is true' \
		2>/dev/null || echo "?")
	if [ "$front" != "TradeAgents" ]; then
		echo "Refusing to capture: '$front' is frontmost, not TradeAgents." >&2
		return 1
	fi

	screencapture -x -o -R "$expected" "$OUT"
	echo "$OUT"
}

# Select a sidebar section, by name or by 1-based index.
#
# An index is accepted because the app is bilingual: matching "Settings" fails outright
# once the reader switches to Chinese, where the row reads 设置. Rather than teach this
# script every string in both languages — a second copy of the translation table, which
# would drift — a number addresses the row regardless of language.
#
# Selection goes through the accessibility tree, never a screen coordinate. Coordinates
# drift the moment a toolbar item or a back button changes the layout, and a click that
# lands somewhere unintended can move focus to another application entirely — after
# which a capture photographs *that* application.
select_section() {
	local wanted="$1"
	if [[ "$wanted" =~ ^[0-9]+$ ]]; then
		osascript <<-APPLESCRIPT 2>&1
			tell application "System Events"
				tell process "TradeAgents"
					set theOutline to outline 1 of scroll area 1 of group 1 of ¬
						splitter group 1 of group 1 of window 1
					if (count of rows of theOutline) < $wanted then
						return "no row $wanted"
					end if
					set selected of row $wanted of theOutline to true
					return "ok"
				end tell
			end tell
		APPLESCRIPT
		return
	fi
	osascript <<-APPLESCRIPT 2>&1
		tell application "System Events"
			tell process "TradeAgents"
				set theOutline to outline 1 of scroll area 1 of group 1 of ¬
					splitter group 1 of group 1 of window 1
				repeat with r in rows of theOutline
					if (value of static text 1 of UI element 1 of r) is "$wanted" then
						set selected of r to true
						return "ok"
					end if
				end repeat
				return "no such section: $wanted"
			end tell
		end tell
	APPLESCRIPT
}

case "${1:-shot}" in
	build) build ;;
	run)   build; relaunch ;;
	shot)  build; relaunch; sleep 8; shot ;;
	section)
		# Screenshot one section of an already-running app, without rebuilding.
		[ -n "${2:-}" ] || { echo "usage: $0 section <Name> [out.png]" >&2; exit 2; }
		OUT="${3:-/tmp/tradeagents-$(echo "$2" | tr 'A-Z' 'a-z').png}"
		osascript -e 'tell application "TradeAgents" to activate' >/dev/null 2>&1 || true
		sleep 1
		result=$(select_section "$2")
		[ "$result" = "ok" ] || { echo "$result" >&2; exit 1; }
		sleep 7
		shot
		;;
	ios)
		# Type-check only, for iOS.
		#
		# A real `xcodebuild ... -sdk iphoneos build` cannot succeed on this machine,
		# and not because of the Swift: actool refuses to compile the asset catalog
		# for any iphone* SDK without a simulator runtime installed, so the build dies
		# on the app icon. Type-checking the sources directly skips actool entirely and
		# is therefore the honest check of whether the *code* is iOS-clean.
		SDK=$(xcrun --sdk iphoneos --show-sdk-path)
		find TradeAgents -name '*.swift' -print0 |
			xargs -0 xcrun swiftc -typecheck -sdk "$SDK" \
				-target arm64-apple-ios17.0 -swift-version 5
		echo "iOS type-check clean."
		;;
	install)
		# Build Release and install into /Applications.
		#
		# Release rather than Debug for two reasons beyond speed: a Debug build carries
		# `get-task-allow`, which is a debugging entitlement that has no business on an
		# app you actually use; and `SMAppService` ("Open at login") will only launch an
		# app from a stable location, so an app left in `build/` cannot register.
		#
		# Signed ad-hoc (`-`), which is what the project already does for macOS. That is
		# enough for this Mac to run and to register a login item; it is *not* enough to
		# distribute — that needs a Developer ID certificate and notarisation.
		xcodebuild -project TradeAgents.xcodeproj \
			-target TradeAgents -configuration Release -quiet \
			-sdk macosx ARCHS=arm64 ONLY_ACTIVE_ARCH=NO build
		pkill -f "TradeAgents.app/Contents/MacOS/TradeAgents" 2>/dev/null || true
		sleep 1
		rm -rf "/Applications/TradeAgents.app"
		cp -R "build/Release/TradeAgents.app" "/Applications/TradeAgents.app"
		# Re-sign after the copy. Ad-hoc signatures cover the bundle's contents by path,
		# and `cp -R` can perturb extended attributes enough for Gatekeeper to consider
		# the copy damaged — re-signing in place is cheaper than debugging that later.
		codesign --force --sign - --entitlements TradeAgents.entitlements \
			"/Applications/TradeAgents.app" 2>/dev/null || true
		echo "installed /Applications/TradeAgents.app"
		;;
	*) echo "usage: $0 {build|run|shot|section|ios|install} [out.png]" >&2; exit 2 ;;
esac
