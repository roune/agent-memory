#!/bin/sh
# Install the agent-memory CLI with uv, for people who have neither uv nor
# Homebrew:
#
#   curl -LsSf https://github.com/roune/agent-memory/releases/latest/download/install.sh | sh
#   curl -LsSf https://github.com/roune/agent-memory/releases/latest/download/install.sh | sh -s -- --yes
#
# The release job fills in the version below, so a copy from a release names
# the version it came with. A copy from a clone needs --version.
#
# This script installs uv if you agree, then runs `uv tool install`. It never
# runs `agent-memory setup` or `agent-memory install`, and writes nothing
# outside uv's own folders: uv's installer is told not to edit shell rc files.

set -eu

VERSION='@VERSION@'
UV_INSTALLER='https://astral.sh/uv/install.sh'
WSL_DOCS='https://github.com/roune/agent-memory/blob/main/docs/deploy.md#windows-wsl-2'

die() {
  echo "install.sh: $*" >&2
  exit 1
}

yes=
while [ $# -gt 0 ]; do
  case $1 in
    --yes | -y) yes=1 ;;
    --version)
      [ $# -ge 2 ] || die "--version needs a value"
      VERSION=$2
      shift
      ;;
    --version=*) VERSION=${1#--version=} ;;
    *) die "unknown option: $1 (options: --yes, --version <version>)" ;;
  esac
  shift
done

case $(uname -s) in
  MINGW* | MSYS* | CYGWIN*)
    cat >&2 <<EOT
agent-memory runs on Linux and macOS, and on Windows inside WSL 2. This shell is
native Windows. Open a WSL 2 shell and run this script there:
  $WSL_DOCS
EOT
    exit 1
    ;;
esac

case $VERSION in
  '@'*) die "this copy has no version filled in. Use the install.sh from a release, or pass --version <version>." ;;
esac

# Under `curl | sh` stdin is the script, so a question is read from the
# terminal. The redirect is tried, because /dev/tty can look readable and
# still fail to open when the process has no controlling terminal.
have_tty() {
  [ -r /dev/tty ] && ( : </dev/tty ) 2>/dev/null
}

uv_install_command="curl -LsSf $UV_INSTALLER | env UV_NO_MODIFY_PATH=1 sh"

install_uv() {
  command -v curl >/dev/null 2>&1 || die "curl is needed to install uv. Install uv yourself: https://docs.astral.sh/uv/getting-started/installation/"
  # UV_NO_MODIFY_PATH=1 keeps uv's installer from editing shell rc files.
  curl -LsSf "$UV_INSTALLER" | env UV_NO_MODIFY_PATH=1 sh
  # It put uv in its default folder, which may not be on PATH yet.
  PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || die "uv was installed but isn't on PATH. Open a new shell and run this script again."
}

if ! command -v uv >/dev/null 2>&1; then
  if [ -z "$yes" ]; then
    if have_tty; then
      printf 'uv is not installed. Install it with its own installer (%s)? [y/N] ' "$UV_INSTALLER" >/dev/tty
      answer=
      read -r answer </dev/tty || answer=
      case $answer in
        y | Y | yes | YES | Yes) ;;
        *) die "uv is needed. Install it, then run this script again: $uv_install_command" ;;
      esac
    else
      echo "install.sh: uv is not installed, and there is no terminal to ask on." >&2
      echo "Install uv with:" >&2
      echo "  $uv_install_command" >&2
      echo "then run this script again, or run it with --yes: ... | sh -s -- --yes" >&2
      exit 1
    fi
  fi
  install_uv
fi

uv tool install "roune-agent-memory==$VERSION"

bin_dir=$(uv tool dir --bin)

echo
echo "agent-memory $VERSION is installed. Next:"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    echo
    echo "  $bin_dir is not on your PATH. Add it for this shell, and to your shell's startup file:"
    echo "    export PATH=\"$bin_dir:\$PATH\""
    ;;
esac
echo
echo "  agent-memory setup      # starts the server with Docker"
echo "  agent-memory install    # asks before it writes any editor hook"
