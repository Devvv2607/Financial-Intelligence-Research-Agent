"""pre-commit hook: run the frontend's ESLint on the staged frontend files only.

pre-commit passes repo-relative paths ("frontend/app/page.tsx"); ESLint's flat config
lives in frontend/, so it has to run there with paths relative to it. A Python wrapper
(not a shell one-liner) so the hook behaves the same in PowerShell, Git Bash and CI.
"""

import shutil
import subprocess
import sys
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def main(paths: list[str]) -> int:
    files = [str(Path(p).resolve().relative_to(FRONTEND)) for p in paths]
    if not files:
        return 0
    # Windows: the extensionless "npx" on PATH is a Unix shell shim; use npx.cmd.
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx is None:
        print("eslint hook: npx not found (install Node.js, then `npm --prefix frontend ci`)")
        return 1
    # --no-warn-ignored: generated files are ignored by the config; don't warn about them.
    cmd = [npx, "--no-install", "eslint", "--max-warnings=0", "--no-warn-ignored", *files]
    # No shell; the arguments are the resolved npx path and repo file paths from pre-commit.
    return subprocess.run(cmd, cwd=FRONTEND, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
