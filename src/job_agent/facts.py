"""Build facts.yaml from master_facts.md.

master_facts.md is the user's own file. Every line that states a fact carries
a status tag:

    [V]  verified against a primary document
    [B]  taken from a note the user has not yet confirmed (usable in drafts)
    [?]  unknown (may not appear in any document until the user resolves it)

The builder turns each fact into one record with a stable id. Records use one
of four statuses: ``V``, ``B``, ``U`` (unknown, from ``[?]`` or from a line
with no tag) and ``N`` (listed as not claimed, for example a skill with no
evidence).

Usage:
    python -m job_agent.facts master_facts.md -o facts.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

STATUSES = ("V", "B", "U", "N")
USABLE_STATUSES = ("V", "B")

_TAG = re.compile(r"\[(V|B|\?)\]")
_SECTION = re.compile(r"^##\s+(?:\d+\.\s*)?(?P<title>.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(?P<body>.+?)\s*$")
_BOLD_LEAD = re.compile(r"^\*\*(?P<head>.+?)\*\*\s*(?P<rest>.*)$")
_DATES = re.compile(r"^(?:\d{1,2}/)?\d{4}(?:\s+to\s+(?:(?:\d{1,2}/)?\d{4}|present))?$", re.I)
_NOT_CLAIMED = re.compile(r"not\s+evidenced|not\s+claimed", re.I)


@dataclass
class Fact:
    id: str
    section: str
    text: str
    status: str
    note: str = ""
    kind: str = "line"
    parent: str | None = None
    evidence: str = ""
    employer: str = ""
    location: str = ""
    title: str = ""
    dates: str = ""

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _split_status(body: str) -> tuple[str, str, str]:
    """Return (text, status, note) for a line that may carry a status tag."""
    match = _TAG.search(body)
    if not match:
        return body.strip(), "U", "no status tag"
    tag = match.group(1)
    text = body[: match.start()].strip()
    note = body[match.end() :].strip()
    return text, ("U" if tag == "?" else tag), note


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", line.strip()))


def _parse_job_header(head: str) -> dict[str, str]:
    """Best-effort split of 'Employer, Place. Title, 06/2023 to 08/2025.'"""
    head = head.strip().rstrip(".")
    if ". " not in head:
        return {}
    left, right = head.split(". ", 1)
    parts: dict[str, str] = {}
    employer, _, location = left.partition(", ")
    parts["employer"] = employer.strip()
    if location:
        parts["location"] = location.strip()
    title, _, dates = right.rpartition(", ")
    if title and _DATES.match(dates.strip()):
        parts["title"] = title.strip()
        parts["dates"] = dates.strip()
    else:
        parts["title"] = right.strip()
    return parts


def parse_master_facts(text: str) -> list[Fact]:
    """Parse the markdown of master_facts.md into Fact records."""
    facts: list[Fact] = []
    section_titles: dict[str, str] = {}
    counters: Counter[str] = Counter()
    section = ""
    current_job: str | None = None
    table_header: list[str] | None = None
    table_state = 0  # 0 none, 1 saw header, 2 in body

    def add(**kwargs) -> Fact:
        counters[section] += 1
        fact = Fact(id=f"{section}-{counters[section]:02d}", section=section, **kwargs)
        facts.append(fact)
        return fact

    for line in text.splitlines():
        heading = _SECTION.match(line)
        if heading:
            title = heading.group("title")
            first = _slug(title.split()[0])
            if first in section_titles and section_titles[first] != title:
                first = _slug(title)
            section_titles[first] = title
            section = first
            current_job = None
            table_header, table_state = None, 0
            continue
        if not section:
            continue  # legend and title lines before the first section

        stripped = line.strip()
        if stripped.startswith("|"):
            if table_state == 0:
                table_header, table_state = _split_row(stripped), 1
            elif table_state == 1:
                table_state = 2 if _is_separator(stripped) else 1
            else:
                _add_table_row(add, table_header or [], _split_row(stripped))
            continue
        table_header, table_state = None, 0

        lead = _BOLD_LEAD.match(stripped)
        if lead:
            body, status, note = _split_status(lead.group("rest"))
            head = lead.group("head").strip()
            fact = add(
                text=f"{head} {body}".strip(),
                status=status,
                note=note,
                kind="heading_line",
                **_parse_job_header(head),
            )
            current_job = fact.id
            continue

        bullet = _BULLET.match(line)
        if bullet:
            body, status, note = _split_status(bullet.group("body"))
            add(text=body, status=status, note=note, kind="bullet", parent=current_job)

    return facts


def _add_table_row(add, header: list[str], cells: list[str]) -> None:
    names = [h.lower() for h in header]
    if "status" in names:
        status_at = names.index("status")
        body = " | ".join(c for c in cells[1:status_at] if c)
        _, status, note = _split_status(cells[status_at] if status_at < len(cells) else "")
        add(text=f"{cells[0]}: {body}".rstrip(": ").strip(), status=status, note=note, kind="table_row")
        return
    evidence = ""
    if "evidence" in names and names.index("evidence") < len(cells):
        evidence = cells[names.index("evidence")]
    body = cells[1] if len(cells) > 1 else ""
    not_claimed = bool(_NOT_CLAIMED.search(cells[0])) or evidence.strip().lower() in ("", "none")
    add(
        text=f"{cells[0]}: {body}".strip(),
        status="N" if not_claimed else "B",
        note="not claimed: no evidence" if not_claimed else "",
        kind="table_row",
        evidence=evidence,
    )


def facts_to_yaml(facts: list[Fact]) -> str:
    records = []
    for fact in facts:
        record = {k: v for k, v in asdict(fact).items() if v not in ("", None)}
        records.append(record)
    header = (
        "# Built from master_facts.md by job_agent.facts. Private: do not commit.\n"
        "# Status: V verified, B unconfirmed note, U unknown, N not claimed.\n"
    )
    return header + yaml.safe_dump({"facts": records}, sort_keys=False, allow_unicode=True, width=100)


def build_facts_file(master_facts: Path | str, out: Path | str) -> list[Fact]:
    master_facts, out = Path(master_facts), Path(out)
    if master_facts.resolve() == out.resolve():
        raise ValueError("output path must differ from the input path")
    facts = parse_master_facts(master_facts.read_text(encoding="utf-8"))
    out.write_text(facts_to_yaml(facts), encoding="utf-8")
    return facts


def load_facts(path: Path | str) -> list[Fact]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    allowed = {f.name for f in fields(Fact)}
    result = []
    for record in raw.get("facts", []):
        unknown = set(record) - allowed
        if unknown:
            raise ValueError(f"unknown fact fields: {sorted(unknown)}")
        result.append(Fact(**record))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build facts.yaml from master_facts.md")
    parser.add_argument("master_facts", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("facts.yaml"))
    args = parser.parse_args(argv)
    facts = build_facts_file(args.master_facts, args.out)
    counts = Counter(f.status for f in facts)
    summary = ", ".join(f"{s}={counts.get(s, 0)}" for s in STATUSES)
    print(f"{len(facts)} facts written to {args.out} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
