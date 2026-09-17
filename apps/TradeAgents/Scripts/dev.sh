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
	# $1, when given, is an AppSection raw value. AppKit folds `--args -Key value`
	# into UserDefaults, which is how the app is told which section to open on.
	if [ -n "${1:-}" ]; then
		open "$APP" --args -TradeAgentsSection "$1"
	else
		open "$APP"
	fi
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
	local geom expected front attempt
	expected="$RECT_X,$RECT_Y,$RECT_W,$RECT_H"

	# Retried, because every precondition here is something another application can
	# take away a fraction of a second after it was checked. On a working machine a
	# notification, a build finishing, or a chat window opening steals focus; the
	# checks below are still worth keeping — capturing the wrong app is the failure
	# this whole function exists to prevent — but failing the run outright on a
	# transient steal just means re-running it by hand. So: re-assert, re-check, and
	# only give up after several rounds, reporting the last reason.
	for attempt in 1 2 3 4 5; do
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

		if ! geom=$(osascript -e \
			'tell application "System Events" to tell process "TradeAgents" to get {position, size} of window 1' \
			2>&1); then
			front="no TradeAgents window (${geom})"
			continue
		fi

		geom=$(echo "$geom" | tr -d ' ')
		if [ "$geom" != "$expected" ]; then
			front="window is at '$geom', expected '$expected' (secondary display?)"
			continue
		fi

		front=$(osascript -e \
			'tell application "System Events" to get name of first process whose frontmost is true' \
			2>/dev/null || echo "?")
		if [ "$front" != "TradeAgents" ]; then
			front="'$front' is frontmost, not TradeAgents"
			continue
		fi

		screencapture -x -o -R "$expected" "$OUT"
		echo "$OUT"
		return 0
	done

	echo "Refusing to capture after 5 attempts: $front" >&2
	echo "If this is a window-position failure the app may be opening on a secondary" >&2
	echo "display; if it is an Accessibility failure, grant this terminal permission" >&2
	echo "in System Settings > Privacy & Security." >&2
	return 1
}

# The sidebar sections, in the order AppSection declares them. Used only to map a
# 1-based index or a case-insensitive name onto the raw value the app expects.
#
# There is no AppleScript path here any more, and that is the point. Driving the
# sidebar from outside the process cannot be done reliably: SwiftUI's List rows expose
# no AXPress action, `set selected of row N to true` mutates the accessibility tree
# without moving the real selection, and a synthetic click at the row's own AX
# coordinates is ignored as well. All three report success and leave the app on
# whatever section it launched with, so the script cheerfully photographed the wrong
# screen and named the file after the right one. The app now takes the section as a
# launch argument instead, which either works or fails visibly.
SECTIONS=(agents markets dca valuation holdings13f fed rates sentiment settings)

resolve_section() {
	local wanted="$1"
	if [[ "$wanted" =~ ^[0-9]+$ ]]; then
		if [ "$wanted" -lt 1 ] || [ "$wanted" -gt "${#SECTIONS[@]}" ]; then
			echo "no section $wanted (there are ${#SECTIONS[@]})" >&2
			return 1
		fi
		echo "${SECTIONS[$((wanted - 1))]}"
		return 0
	fi
	local lower
	lower=$(echo "$wanted" | tr 'A-Z' 'a-z')
	for s in "${SECTIONS[@]}"; do
		if [ "$s" = "$lower" ]; then echo "$s"; return 0; fi
	done
	echo "no such section: $wanted (one of: ${SECTIONS[*]})" >&2
	return 1
}

case "${1:-shot}" in
	build) build ;;
	run)   build; relaunch ;;
	shot)  build; relaunch; sleep 8; shot ;;
	section)
		# Relaunch straight onto one section and screenshot it.
		#
		# Relaunching rather than navigating is not a compromise: it is the only way
		# the requested section is guaranteed to be the one captured. See the note
		# above resolve_section.
		[ -n "${2:-}" ] || {
			echo "usage: $0 section <name|index> [out.png]" >&2
			echo "sections: ${SECTIONS[*]}" >&2
			exit 2
		}
		sec=$(resolve_section "$2") || exit 1
		OUT="${3:-/tmp/tradeagents-$sec.png}"
		build
		relaunch "$sec"
		sleep 8
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
