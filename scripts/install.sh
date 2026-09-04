#!/bin/sh

set -eu

SIDE_DOG_REQUIREMENT='side-dog @ git+https://github.com/qfennessy/side-dog.git@main'
UV_INSTALL_URL='https://docs.astral.sh/uv/getting-started/installation/'

fail() {
    printf '%s\n' "Side Dog install failed: $*" >&2
    exit 1
}

if [ "$#" -ne 0 ]; then
    fail "this script does not accept arguments"
fi

command -v git >/dev/null 2>&1 ||
    fail "Git is required; install Git and run this script again"
git --version >/dev/null 2>&1 ||
    fail "Git was found but could not run; repair Git and try again"
command -v uv >/dev/null 2>&1 ||
    fail "uv is required; install it from $UV_INSTALL_URL"
uv --version >/dev/null 2>&1 ||
    fail "uv was found but could not run; reinstall it from $UV_INSTALL_URL"

printf '%s\n' "Installing the latest Side Dog from main..."
if ! uv tool install --force --refresh "$SIDE_DOG_REQUIREMENT"; then
    fail "uv could not install Side Dog; check the uv error above and retry"
fi

if ! tool_bin_dir=$(uv tool dir --bin) || [ -z "$tool_bin_dir" ]; then
    fail "uv installed Side Dog but did not report its executable directory"
fi

side_dog_bin=$tool_bin_dir/side-dog
if [ ! -x "$side_dog_bin" ]; then
    fail "uv completed, but no executable was found at $side_dog_bin"
fi

if ! installed_version=$("$side_dog_bin" --version); then
    fail "the installed Side Dog executable did not start"
fi
case "$installed_version" in
    side-dog\ *) ;;
    *) fail "the installed executable returned an unexpected version: $installed_version" ;;
esac

printf '%s\n' "Installed $installed_version."
case ":${PATH:-}:" in
    *:"$tool_bin_dir":*) ;;
    *)
        printf '%s\n' "Side Dog is installed in $tool_bin_dir, which is not on PATH."
        printf '%s\n' "Run 'uv tool update-shell', then open a new terminal."
        ;;
esac

printf '%s\n' "Next, open a project and run: side-dog doctor ."
