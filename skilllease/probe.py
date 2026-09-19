"""Observe checks inside the target interpreter. Python 3.7+.

Reads a JSON list of checks on stdin, prints a JSON list of observations in
the same order. Each check runs in its own child process so a failing or
hanging import cannot affect the others.
"""

import builtins
import importlib
try:
    from importlib import metadata
except ImportError:
    import importlib_metadata as metadata
import inspect
import json
import os
import subprocess
import sys

DEFAULT_TIMEOUT = 20.0


def observe_package(check):
    try:
        return {"version": metadata.version(check["dist"]), "error": None}
    except metadata.PackageNotFoundError:
        return {"version": None, "error": None}


def observe_symbol(check):
    module, qualname = check["module"], check["qualname"]
    out = {"exists": False, "signature": None, "params": [], "absent": None, "error": None}
    try:
        obj = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name and (module == e.name or module.startswith(e.name + ".")):
            out["absent"] = f"module {module} is not installed"
            return out
        out["error"] = f"import {module} failed: {type(e).__name__}: {e}"
        return out
    except BaseException as e:
        out["error"] = f"import {module} failed: {type(e).__name__}: {e}"
        return out
    path = module
    for part in qualname.split("."):
        if not hasattr(obj, part):
            out["absent"] = f"{path} has no attribute '{part}'"
            return out
        obj = getattr(obj, part)
        path = f"{path}.{part}"
    out["exists"] = True
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return out
    out["signature"] = str(sig)
    out["params"] = list(sig.parameters)
    return out


def observe_predicate(check):
    try:
        code = compile(check["expr"], "<bound-predicate>", "eval")
    except (SyntaxError, ValueError, TypeError) as e:
        return {"error": f"invalid predicate: {type(e).__name__}: {e}"}
    try:
        value = eval(code, {"__builtins__": builtins, "__name__": "__main__"})
    except Exception as e:
        return {"raised": {"type": type(e).__name__, "module": type(e).__module__, "message": str(e)}}
    except BaseException as e:
        return {"error": f"predicate interrupted: {type(e).__name__}: {e}"}
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        value = repr(value)
    return {"value": value, "error": None}


OBSERVERS = {"package": observe_package, "symbol": observe_symbol, "predicate": observe_predicate}


def observe(check):
    fn = OBSERVERS.get(check.get("type"))
    if fn is None:
        return {"error": f"unknown check type: {check.get('type')!r}"}
    return fn(check)


def observe_isolated(check, timeout):
    cmd = [sys.executable, os.path.abspath(__file__), "--one"]
    try:
        proc = subprocess.run(cmd, input=json.dumps(check), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout:g}s"}
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
        return {"error": f"probe exited {proc.returncode}: {tail[0]}"}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": f"probe printed invalid JSON: {proc.stdout[:200]!r}"}


def main(argv):
    # The script directory is on sys.path when run by path; drop it so a target
    # module named model, cli, or runner is not shadowed by skilllease's files.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
    if "--one" in argv:
        print(json.dumps(observe(json.load(sys.stdin))))
        return 0
    timeout = DEFAULT_TIMEOUT
    if "--timeout" in argv:
        timeout = float(argv[argv.index("--timeout") + 1])
    checks = json.load(sys.stdin)
    print(json.dumps([observe_isolated(c, timeout) for c in checks]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
