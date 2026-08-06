from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys

from app.config import BASE_DIR


ENV_PATH = BASE_DIR / ".env"


def read_env(path: Path) -> list[str]:
    if not path.exists():
        example = BASE_DIR / ".env.example"
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")
    return path.read_text(encoding="utf-8").splitlines()


def upsert(lines: list[str], values: dict[str, str]) -> list[str]:
    found: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={values[key]}")
                found.add(key)
                continue
        output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key, value in values.items():
        if key not in found:
            output.append(f"{key}={value}")
    return output


def save_values(values: dict[str, str]) -> None:
    lines = read_env(ENV_PATH)
    ENV_PATH.write_text("\n".join(upsert(lines, values)) + "\n", encoding="utf-8")


def setup() -> int:
    print("\nALPACA-PAPER VERBINDEN")
    print("Die Eingaben werden nur lokal in deiner .env-Datei gespeichert.")
    print("Benutze ausschließlich die Keys des 250-USD-Paperkontos.\n")

    api_key = input("Paper API Key ID: ").strip()
    secret = getpass("Paper Secret Key (beim Einfügen unsichtbar): ").strip()
    if not api_key or not secret:
        print("\nFehler: Beide Keys werden benötigt.")
        return 1

    save_values(
        {
            "BROKER_MODE": "alpaca",
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret,
            "ALPACA_PAPER": "true",
            "ALPACA_ORDER_EXECUTION": "false",
            "ALPACA_ALLOW_SHORTS": "false",
            "ALPACA_MAX_ORDER_NOTIONAL": "75",
            "ALPACA_DATA_FEED": "iex",
            "ALPACA_REQUIRE_FRACTIONABLE": "true",
            "MAX_OPEN_POSITIONS": "2",
            "MAX_POSITION_FRACTION": "0.30",
        }
    )

    # Import after writing .env so config sees the new values in a fresh process.
    print("\nKeys gespeichert. Prüfe jetzt die Verbindung ...")
    return 0


def set_orders(enabled: bool) -> int:
    save_values({"ALPACA_ORDER_EXECUTION": "true" if enabled else "false"})
    print(
        "Alpaca-Paper-Orders sind jetzt AKTIVIERT."
        if enabled
        else "Alpaca-Paper-Orders sind jetzt GESPERRT."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--disable-orders", action="store_true")
    args = parser.parse_args()
    if args.enable_orders:
        return set_orders(True)
    if args.disable_orders:
        return set_orders(False)
    return setup()


if __name__ == "__main__":
    raise SystemExit(main())
