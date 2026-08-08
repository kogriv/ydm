#!/usr/bin/env python3
"""Interactive prompts for YDM menu (stdlib, testable)."""
from __future__ import annotations

import sys
from typing import Callable, Iterable, List, Optional


Reader = Callable[[str], str]


def default_reader(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return "q"


def prompt_line(prompt: str, *, reader: Reader = default_reader) -> str:
    return reader(prompt).strip()


def prompt_int(
    prompt: str,
    *,
    default: Optional[int] = None,
    reader: Reader = default_reader,
) -> Optional[int]:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = prompt_line(f"{prompt}{suffix}: ", reader=reader)
        if raw == "" and default is not None:
            return default
        if raw.lower() in {"q", "0"}:
            return None
        if raw.isdigit():
            return int(raw)
        print("Enter a number, 0 or q to cancel.")


def prompt_ints(
    prompt: str,
    *,
    max_n: int,
    reader: Reader = default_reader,
) -> Optional[List[int]]:
    while True:
        raw = prompt_line(f"{prompt}: ", reader=reader).lower()
        if raw in {"", "q", "0"}:
            return None
        if raw == "all":
            return list(range(1, max_n + 1))
        parts = [p.strip() for p in raw.replace(" ", "").split(",") if p.strip()]
        if not parts or not all(p.isdigit() for p in parts):
            print("Enter numbers like 1 or 1,2 or 'all', 0/q to cancel.")
            continue
        values = [int(p) for p in parts]
        if any(v < 1 or v > max_n for v in values):
            print(f"Choose 1..{max_n}")
            continue
        return sorted(set(values))


def prompt_yes_no(
    prompt: str,
    *,
    default: bool = False,
    reader: Reader = default_reader,
) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = prompt_line(f"{prompt} [{hint}]: ", reader=reader).lower()
    if raw == "":
        return default
    return raw in {"y", "yes"}


def prompt_confirm_name(expected: str, *, reader: Reader = default_reader) -> bool:
    print(f"Type '{expected}' to confirm:")
    raw = prompt_line("> ", reader=reader)
    return raw == expected


def pause(message: str = "Press Enter to continue...", *, reader: Reader = default_reader) -> None:
    if sys.stdin.isatty():
        reader(message)


def pick_from_list(
    title: str,
    items: Iterable[str],
    *,
    reader: Reader = default_reader,
) -> Optional[int]:
    rows = list(items)
    if not rows:
        print(f"{title}: (empty)")
        return None
    print(title)
    for index, label in enumerate(rows, start=1):
        print(f" {index:2d}  {label}")
    print("  0  Back")
    return prompt_int("Choose", reader=reader)
