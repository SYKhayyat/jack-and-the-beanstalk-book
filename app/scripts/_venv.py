"""Make `python -m app.scripts.<name>` work with whatever `python` you typed.

The project's dependencies live in `.venv`, so the honest command is
`.venv/bin/python -m app.scripts.make_plates`. Typing that is easy to forget,
and forgetting it produces a bare `ModuleNotFoundError: No module named 'PIL'`
that says nothing about the real problem.

`ensure_venv()` fixes that by doing what a person would do: if this interpreter
cannot import the project's dependencies and the project's virtualenv is right
there, hand the same command to that interpreter instead. One process
boundary, no state, no surprises — and running under the right interpreter (or
on a machine where the deps are already global, like CI) changes nothing at all.

It is safe to call from every operator script and must be called *before* any
`app.services` import, because the failure it repairs happens at that import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ensure_venv"]

#: Set on the re-exec'd process so a broken virtualenv cannot loop forever.
_REEXEC_MARKER = "JACK_BEANSTALK_VENV_REEXEC"

#: Any of these missing means this is not the project's interpreter.
_PROJECT_DEPENDENCIES = ("PIL", "fastapi", "sqlalchemy", "pydantic")


def _venv_python() -> Path | None:
    """The project's virtualenv interpreter, when this checkout has one."""
    root = Path(__file__).resolve().parents[2]  # app/scripts/_venv.py -> project
    for relative in ("bin/python", "Scripts/python.exe"):
        candidate = root / ".venv" / relative
        if candidate.exists():
            return candidate
    return None


def _missing_dependency() -> str | None:
    """The first project dependency this interpreter cannot import, if any."""
    for module in _PROJECT_DEPENDENCIES:
        try:
            __import__(module)
        except ImportError:
            return module
    return None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_name(caller_file: str) -> str | None:
    """`app.scripts.plate_prompts` for the file that called `ensure_venv`.

    Derived from the calling file's path, not from `sys.argv` (which holds the
    user's arguments) and not from `sys.modules["__main__"]` (which is not this
    script at all under a test runner). Returns None when the file is not inside
    this checkout, so the caller falls back to re-running it by path.
    """
    try:
        relative = Path(caller_file).resolve().relative_to(_project_root())
    except ValueError:
        return None
    if relative.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _reexec_argv(interpreter: Path, caller_file: str, args: list[str]) -> list[str]:
    """`interpreter -m <this module> <the user's own arguments>`."""
    module = _module_name(caller_file)
    if module is not None:
        return [str(interpreter), "-m", module, *args]
    return [str(interpreter), str(Path(caller_file).resolve()), *args]


def ensure_venv(caller_file: str | None = None) -> None:
    """Re-run this command under `.venv/bin/python` when it is missing deps.

    Returns normally when this interpreter is fine, or when there is no
    virtualenv to switch to — then the caller's own import error is the honest
    message. Replaces this process after handing over, so the caller's module
    body never runs twice.

    Args:
        caller_file: The script that wants the venv. Discovered from the calling
            frame when omitted; a parameter so the behaviour can be tested
            without spawning a process.
    """
    if os.environ.get(_REEXEC_MARKER):
        return
    missing = _missing_dependency()
    if missing is None:
        return
    interpreter = _venv_python()
    if interpreter is None:
        return
    print(
        f"This python has no {missing}; using the project's virtualenv instead.",
        file=sys.stderr,
    )
    if caller_file is None:
        caller_file = str(sys._getframe(1).f_globals.get("__file__", sys.argv[0]))
    os.environ[_REEXEC_MARKER] = "1"
    os.execv(str(interpreter), _reexec_argv(interpreter, caller_file, sys.argv[1:]))
