"""
tool.py — Interactive wizard for running the doc-scraper toolchain.

Run with:  py tool.py

The wizard remembers the choices you made in the last run and pre-fills
them as defaults, so you only need to enter values when something changes.
Settings are stored in .toolstate (JSON) next to this file.
"""

import json
import os
import subprocess
import sys

# ---------------------------------------------------------------------------
# State file (persists last-run settings between sessions)
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".toolstate")

DEFAULT_STATE = {
    "crawler": {
        "start_url": "https://docs.microfocus.com/doc/386/25.4/home",
        "test_seconds": 0,
        "edge_driver_path": "",
    },
    "downloader": {
        "edges_csv": os.path.join("source", "doc_links_edges.csv"),
        "output_dir": "docs",
        "test_seconds": 0,
        "force_download": False,
        "page_timeout": 30,
        "edge_driver_path": "",
    },
}


def load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            # Merge with defaults so new keys added in future versions appear
            state = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
            for section in state:
                if section in saved:
                    state[section].update(saved[section])
            return state
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        print(f"  [warn] Could not save settings: {exc}")


def run_command(cmd: list[str], env: dict) -> bool:
    result = subprocess.run(cmd, env=env)
    if result.returncode == 0:
        print("\n  Tool completed successfully.")
        return True

    print(f"\n  Tool failed with exit code {result.returncode}.")
    return False


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _yn(prompt: str, default: bool) -> bool:
    """Ask a yes/no question; return bool."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{prompt} {hint}: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _ask(prompt: str, default: str) -> str:
    """Ask for a string value with a default."""
    display = f" (default: {default})" if default else ""
    raw = input(f"{prompt}{display}: ").strip()
    return raw if raw else default


def _ask_int(prompt: str, default: int) -> int:
    """Ask for an integer with a default."""
    while True:
        raw = input(f"{prompt} (default: {default}): ").strip()
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def _separator(label: str = "") -> None:
    width = 60
    if label:
        print(f"\n{'─' * 4} {label} {'─' * max(0, width - len(label) - 6)}")
    else:
        print("─" * width)


# ---------------------------------------------------------------------------
# Tool 1 – URL Scanner (doc_crawler.py)
# ---------------------------------------------------------------------------

def wizard_crawler(state: dict) -> bool:
    cfg = state["crawler"]

    _separator("Tool 1 · URL Scanner")
    print("Scans the documentation site and produces a CSV with all page links.\n")

    cfg["start_url"] = _ask("Start URL", cfg["start_url"])

    use_test = _yn("Run in test mode (time-capped)?", default=cfg["test_seconds"] > 0)
    if use_test:
        cfg["test_seconds"] = _ask_int("  Time cap (seconds)", cfg["test_seconds"] or 120)
    else:
        cfg["test_seconds"] = 0

    if _yn("Set a custom Edge driver path?", default=bool(cfg["edge_driver_path"])):
        cfg["edge_driver_path"] = _ask("  Edge driver path (msedgedriver.exe)", cfg["edge_driver_path"])
    else:
        cfg["edge_driver_path"] = ""

    # Build environment and command
    env = os.environ.copy()
    env["START_URL"]        = cfg["start_url"]
    env["MAX_RUNTIME_SECONDS"] = str(cfg["test_seconds"])
    if cfg["edge_driver_path"]:
        env["EDGE_DRIVER_PATH"] = cfg["edge_driver_path"]

    cmd = [sys.executable, "doc_crawler.py"]
    _show_command(cmd, env, ["START_URL", "MAX_RUNTIME_SECONDS", "EDGE_DRIVER_PATH"])

    if _yn("\nRun now?", default=True):
        save_state(state)
        return run_command(cmd, env)

    print("  Skipped.")
    return True


# ---------------------------------------------------------------------------
# Tool 2 – Page Downloader (downloader.py)
# ---------------------------------------------------------------------------

def wizard_downloader(state: dict) -> bool:
    cfg = state["downloader"]

    _separator("Tool 2 · Page Downloader")
    print("Downloads every page listed in the edges CSV and saves it as local HTML.\n")

    cfg["edges_csv"]  = _ask("Edges CSV path", cfg["edges_csv"])
    cfg["output_dir"] = _ask("Output directory", cfg["output_dir"])

    use_test = _yn("Run in test mode (time-capped)?", default=cfg["test_seconds"] > 0)
    if use_test:
        cfg["test_seconds"] = _ask_int("  Time cap (seconds)", cfg["test_seconds"] or 45)
    else:
        cfg["test_seconds"] = 0

    cfg["force_download"] = _yn("Force re-download of existing pages?", default=cfg["force_download"])
    cfg["page_timeout"]   = _ask_int("Page load timeout (seconds)", cfg["page_timeout"])

    if _yn("Set a custom Edge driver path?", default=bool(cfg["edge_driver_path"])):
        cfg["edge_driver_path"] = _ask("  Edge driver path (msedgedriver.exe)", cfg["edge_driver_path"])
    else:
        cfg["edge_driver_path"] = ""

    # Build command
    cmd = [sys.executable, "downloader.py"]
    if cfg["test_seconds"] > 0:
        cmd += ["-test", str(cfg["test_seconds"])]
    if cfg["force_download"]:
        cmd += ["--force"]
    cmd += ["--csv",    cfg["edges_csv"]]
    cmd += ["--output", cfg["output_dir"]]

    env = os.environ.copy()
    env["PAGE_LOAD_TIMEOUT"] = str(cfg["page_timeout"])
    if cfg["edge_driver_path"]:
        env["EDGE_DRIVER_PATH"] = cfg["edge_driver_path"]

    _show_command(cmd, env, ["PAGE_LOAD_TIMEOUT", "EDGE_DRIVER_PATH"])

    if _yn("\nRun now?", default=True):
        save_state(state)
        return run_command(cmd, env)

    print("  Skipped.")
    return True


# ---------------------------------------------------------------------------
# Helper: show the full command before running
# ---------------------------------------------------------------------------

def _show_command(cmd: list, env: dict, env_keys: list[str]) -> None:
    """Print the command and any overridden env vars so the user can see exactly what will run."""
    env_display = [
        f"{k}={env[k]}"
        for k in env_keys
        if k in env and env.get(k, "") not in ("", "0", os.environ.get(k, ""))
    ]
    print("\n  Command: " + " ".join(cmd))
    if env_display:
        print("  Env    : " + "  ".join(env_display))


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

MENU_ITEMS = [
    ("Run URL Scanner (doc_crawler.py)",    wizard_crawler),
    ("Run Page Downloader (downloader.py)", wizard_downloader),
    ("Run both in sequence",                None),  # handled inline
]


def main() -> None:
    print("\n╔══════════════════════════════════════╗")
    print("║   Doc Scraper Toolchain Wizard       ║")
    print("╚══════════════════════════════════════╝")

    state = load_state()

    _separator("What would you like to do?")
    for i, (label, _) in enumerate(MENU_ITEMS, start=1):
        print(f"  {i}. {label}")
    print(f"  q. Quit")

    while True:
        choice = input("\nEnter choice: ").strip().lower()
        if choice == "q":
            print("Bye.")
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(MENU_ITEMS):
                break
        except ValueError:
            pass
        print("  Please enter a number from the list, or q to quit.")

    label, fn = MENU_ITEMS[idx]

    overall_ok = True

    if fn is not None:
        overall_ok = fn(state)
        save_state(state)
    else:
        # Run both in sequence
        _separator("Run Both Tools")
        print("First the URL Scanner, then the Page Downloader.\n")
        overall_ok = wizard_crawler(state)
        save_state(state)
        print()
        if overall_ok and _yn("Continue to Page Downloader?", default=True):
            overall_ok = wizard_downloader(state)
            save_state(state)
        elif not overall_ok:
            print("Skipping Page Downloader because the URL Scanner failed.")

    if overall_ok:
        print("\nAll done.\n")
    else:
        print("\nFinished with errors.\n")


if __name__ == "__main__":
    main()
