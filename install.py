"""Set up Shortlist in this folder: python install.py [--yes] [--no-browser]

Creates a virtual environment in .venv, installs the dependencies into it, optionally installs the headless browser
for careers pages that need JavaScript, then runs the setup wizard (python -m shortlist init). Safe to run again: it
reuses .venv and keeps your config.yaml, resume.md, and .env.
"""
# Standard library only, because it runs before anything is installed.
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def run(*args: str) -> None:
    subprocess.run([str(PYTHON), *args], cwd=ROOT, check=True)


def agree(question: str) -> bool:
    """Yes unless answered no. Without a terminal to answer in, the default, as in the setup wizard."""
    try:
        return input(question).strip().lower() in ("", "y", "yes")
    except EOFError:
        return True


def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    yes, browser = "--yes" in argv, "--no-browser" not in argv
    if sys.version_info < (3, 11):
        print(f"Shortlist needs Python 3.11 or newer; this is {sys.version.split()[0]}. Install a newer Python from "
              "https://www.python.org/downloads/ and run this script with it (Windows: py -3.11 install.py).")
        return 1
    try:
        if not PYTHON.exists():
            step(f"Creating a virtual environment in {VENV}")
            try:
                venv.create(VENV, with_pip=True)
            except Exception as e:  # e.g. Debian/Ubuntu without the python3-venv package
                print(f"Could not create it ({e}). On Debian or Ubuntu: sudo apt install python3-venv")
                return 1
        step("Installing dependencies (a minute or two the first time)")
        run("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-r", "requirements.txt")
        if browser and (yes or agree("\nInstall a headless browser for careers pages that need JavaScript "
                                     "(about 150 MB; you can skip it and add it later)? [Y/n] ")):
            step("Installing the headless browser (Chromium for Playwright)")
            run("-m", "playwright", "install", "chromium")
        step("Setup questions")
        run("-m", "shortlist", "init", *(["--yes"] if yes else []))
    except subprocess.CalledProcessError as e:
        print(f"\nThat step failed (exit code {e.returncode}); see the messages above. Fix it and run "
              "python install.py again: finished steps are skipped or quick.")
        return 1
    activate = r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    print(f"\nDone. In a new terminal, run `{activate}` in this folder first; then the commands above work as "
          "written. Or call the environment's Python directly:\n"
          f"  {PYTHON.relative_to(ROOT)} -m shortlist web")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
