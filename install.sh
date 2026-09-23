#!/bin/sh
# Install agent-response-history.
#   ./install.sh              install for every client found on PATH (Claude Code and/or Codex)
#   ./install.sh ARGS...      same flags as install.py: --provider, --uninstall, --rollback, --home
set -eu
# Stay in the caller's directory: relative --home, --rollback, CLAUDE_CONFIG_DIR and CODEX_HOME
# must mean what they mean to install.py.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

[ "$(uname -s)" = Darwin ] || { echo "agent-response-history requires macOS." >&2; exit 1; }

# A usable interpreter must actually run Python and report >= 3.10 (an exit status alone
# would accept echo or true).
supported() { command -v "$1" >/dev/null 2>&1 && [ "$("$1" -c 'import sys; print(sys.version_info >= (3, 10))' 2>/dev/null)" = True ]; }

# An explicit action means no auto-detection. install.py (argparse) also accepts unique
# prefixes of its options (--prov, --unin, --roll=DIR), so recognise those too.
explicit= provider= provider_next=
for arg in "$@"; do
  if [ -n "$provider_next" ]; then provider=$arg; provider_next=; continue; fi
  name=${arg%%=*}
  case $name in
    -h | --help) explicit=1 ;;
    --?*)
      case --provider in "$name"*)
        explicit=1
        case $arg in *=*) provider=${arg#*=} ;; *) provider_next=1 ;; esac ;;
      esac
      case --uninstall in "$name"*) explicit=1 ;; esac
      case --rollback in "$name"*) explicit=1 ;; esac ;;
  esac
done
if [ -z "$explicit" ]; then
  c=0; x=0
  command -v claude >/dev/null 2>&1 && c=1
  command -v codex >/dev/null 2>&1 && x=1
  case $c$x in
    11) provider=both ;;
    10) provider=claude ;;
    01) provider=codex ;;
    *) echo "Neither claude nor codex is on PATH; run ./install.sh --provider claude|codex|both" >&2; exit 1 ;;
  esac
  set -- --provider "$provider" "$@"
fi

# Reuse the Python an existing install registered: an unchanged hook command keeps Codex's
# trust. Codex's hook wins unless only Claude is being installed.
prefer=codex
[ "$provider" = claude ] && prefer=claude
registered=
for probe in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  supported "$probe" || continue
  registered=$("$probe" - "$prefer" "$@" <<'PY' || true
import json, os, shlex, sys
prefer, args, home = sys.argv[1], sys.argv[2:], None
for i, a in enumerate(args):
    name = a.split("=", 1)[0]
    if len(name) >= 4 and "--home".startswith(name):  # argparse accepts --ho, --hom
        home = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else None)
if home:  # install.py --home ignores CLAUDE_CONFIG_DIR / CODEX_HOME
    home = os.path.realpath(os.path.expanduser(home))
    codex, claude = os.path.join(home, ".codex"), os.path.join(home, ".claude")
else:
    home = os.path.realpath(os.path.expanduser("~"))
    codex = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
hook_path = os.path.join(home, ".local/share/agent-response-history/integrations/hook.py")  # as install.py builds it
files = [os.path.join(codex, "hooks.json"), os.path.join(claude, "settings.json")]
for path in files if prefer == "codex" else files[::-1]:
    try:
        groups = json.load(open(path)).get("hooks", {}).values()
        hooks = [h for event in groups for g in event for h in g.get("hooks", [])]
    except (OSError, ValueError, AttributeError, TypeError):
        continue
    for hook in hooks:  # only our own `<python> -I -B <hook>`; any other hook is ignored
        try:
            cmd = shlex.split(hook.get("command", "")) if isinstance(hook, dict) else []
        except (ValueError, TypeError, AttributeError):  # malformed or non-string: not ours
            continue
        if len(cmd) == 4 and cmd[1:3] == ["-I", "-B"] and cmd[3] == hook_path:
            print(cmd[0]); sys.exit()
PY
)
  break
done

PY=
for candidate in "$registered" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  [ -n "$candidate" ] && supported "$candidate" && { PY=$candidate; break; }
done
[ -n "$PY" ] || { echo "Python 3.10 or newer is required." >&2; exit 1; }

exec "$PY" "$SCRIPT_DIR/install.py" "$@"
