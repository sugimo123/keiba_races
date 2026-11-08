"""Extract race and betting history from committed docs/index.html snapshots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pygit2
from bs4 import BeautifulSoup
from bs4.element import Tag

INDEX_PATH = "docs/index.html"
_HEADING_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<venue>.+?)(?P<race>\d+)$"
)
_LEGACY_HEADING_PATTERN = re.compile(r"^\d{12}$")
_NUMERIC_COLUMNS = ("R", "馬番", "単勝", "勝率", "残差")
_TICKET_SEPARATOR_PATTERN = re.compile(r"[\s\-−–—ー－]+")


def _text(element: Tag) -> str:
    return element.get_text(" ", strip=True)


def _parse_heading(
    soup: BeautifulSoup, fallback_date: str | None
) -> tuple[str, str | None, int | None]:
    heading = soup.find("h1")
    if heading is None:
        raise ValueError("Race heading <h1> was not found")

    heading_text = _text(heading)
    match = _HEADING_PATTERN.fullmatch(heading_text)
    if match is not None:
        return match.group("date"), match.group("venue"), int(match.group("race"))
    if _LEGACY_HEADING_PATTERN.fullmatch(heading_text) and fallback_date is not None:
        return fallback_date, None, int(heading_text[-2:])

    raise ValueError(f"Unexpected race heading: {heading_text!r}")


def _parse_horses(
    soup: BeautifulSoup, race_date: str
) -> tuple[list[dict[str, object]], list[str]]:
    table = soup.find("table", class_="dataframe")
    if table is None:
        raise ValueError("Horse information table was not found")

    header_row = table.find("thead")
    source_headers = (
        [_text(cell) for cell in header_row.find_all("th")]
        if header_row is not None
        else []
    )
    if source_headers and not source_headers[0]:
        source_headers = source_headers[1:]
    if not source_headers:
        raise ValueError("Horse information table has no column headers")
    headers = ["単勝" if header == "単" else header for header in source_headers]
    if len(set(headers)) != len(headers):
        raise ValueError("Horse information table has duplicate column headers")

    required = {"開催", "R", "馬番", "馬名", "単勝"}
    missing = required.difference(headers)
    if missing:
        raise ValueError(f"Horse information table is missing columns: {sorted(missing)}")

    rows: list[dict[str, object]] = []
    for row in table.find_all("tbody"):
        for data_row in row.find_all("tr"):
            cells = data_row.find_all("td")
            if not cells:
                continue
            if len(cells) != len(headers):
                raise ValueError(
                    "Horse information row has "
                    f"{len(cells)} cells for {len(headers)} columns"
                )
            values = []
            for cell in cells:
                value = _text(cell)
                values.append(value if value and value.casefold() != "nan" else None)
            rows.append({"日付": race_date, **dict(zip(headers, values))})

    return rows, headers


def _parse_tickets(
    soup: BeautifulSoup, race_date: str, venue: str, race_number: int
) -> list[dict[str, object]]:
    container = soup.find("div", class_="tickets-container")
    if container is None:
        return []

    rows: list[dict[str, object]] = []
    for group in container.find_all("div", class_="bet-group"):
        kind_heading = group.find("h3")
        if kind_heading is None:
            raise ValueError("Bet group has no ticket type heading")
        ticket_type = _text(kind_heading)
        for item in group.find_all("li"):
            ticket_text = _text(item)
            numbers = tuple(int(number) for number in re.findall(r"\d+", ticket_text))
            remainder = re.sub(r"\d+", "", ticket_text)
            if not numbers or (
                remainder and _TICKET_SEPARATOR_PATTERN.fullmatch(remainder) is None
            ):
                raise ValueError(f"Unexpected ticket format: {ticket_text!r}")
            rows.append(
                {
                    "日付": race_date,
                    "競馬場名": venue,
                    "レース番号": race_number,
                    "券種": ticket_type,
                    "買い目": numbers,
                }
            )
    return rows


def _parse_snapshot(
    content: bytes, fallback_date: str | None = None
) -> tuple[list[dict[str, object]], list[str], list[dict[str, object]]]:
    soup = BeautifulSoup(content.decode("utf-8-sig"), "lxml")
    race_date, heading_venue, heading_race = _parse_heading(soup, fallback_date)
    horse_rows, horse_headers = _parse_horses(soup, race_date)
    venue = str(horse_rows[0]["開催"]) if horse_rows else ""
    race_number = int(horse_rows[0]["R"]) if horse_rows else 0
    if heading_venue is not None and heading_venue != venue:
        raise ValueError(
            f"Heading venue {heading_venue!r} does not match table venue {venue!r}"
        )
    if heading_race is not None and heading_race != race_number:
        raise ValueError(
            f"Heading race {heading_race} does not match table race {race_number}"
        )
    ticket_rows = _parse_tickets(soup, race_date, venue, race_number)
    return horse_rows, horse_headers, ticket_rows


def load_history(
    repository_path: str | Path = ".",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return horse-odds and ticket DataFrames from committed race pages.

    For a race present in multiple commits, the newest reachable snapshot wins.
    The worktree is never checked out or modified.
    """
    repository = pygit2.Repository(str(repository_path))
    if repository.head_is_unborn:
        raise ValueError(f"Repository has no commits: {repository_path}")

    horse_rows: list[dict[str, object]] = []
    ticket_rows: list[dict[str, object]] = []
    horse_columns: list[str] = []
    seen_blobs: set[pygit2.Oid] = set()
    seen_races: set[tuple[str, str, int]] = set()

    for commit in repository.walk(
        repository.head.target, pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL
    ):
        try:
            blob = commit.tree / INDEX_PATH
        except KeyError:
            continue
        if not isinstance(blob, pygit2.Blob) or blob.id in seen_blobs:
            continue
        seen_blobs.add(blob.id)
        content = blob.data
        if b"<table" not in content.lower():
            continue

        try:
            commit_date = datetime.fromtimestamp(
                commit.author.time, timezone.utc
            ) + timedelta(minutes=commit.author.offset)
            current_horses, current_columns, current_tickets = _parse_snapshot(
                content, fallback_date=commit_date.date().isoformat()
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError(
                f"Could not parse {INDEX_PATH} in commit {commit.id}: {error}"
            ) from error

        if not current_horses:
            raise ValueError(f"No horse rows found in {INDEX_PATH} at commit {commit.id}")

        first_horse = current_horses[0]
        race_key = (
            str(first_horse["日付"]),
            str(first_horse["開催"]),
            int(first_horse["R"]),
        )
        if race_key in seen_races:
            continue
        seen_races.add(race_key)

        horse_rows.extend(current_horses)
        for column in current_columns:
            if column not in horse_columns:
                horse_columns.append(column)
        ticket_rows.extend(current_tickets)

    horse_frame = pd.DataFrame.from_records(
        horse_rows, columns=["日付", *horse_columns]
    )
    if not horse_frame.empty:
        horse_frame["日付"] = pd.to_datetime(horse_frame["日付"], format="%Y-%m-%d")
        for column in _NUMERIC_COLUMNS:
            if column in horse_frame.columns:
                horse_frame[column] = pd.to_numeric(horse_frame[column], errors="raise")

    ticket_frame = pd.DataFrame.from_records(
        ticket_rows,
        columns=["日付", "競馬場名", "レース番号", "券種", "買い目"],
    )
    if not ticket_frame.empty:
        ticket_frame["日付"] = pd.to_datetime(ticket_frame["日付"], format="%Y-%m-%d")
        ticket_frame["レース番号"] = pd.to_numeric(
            ticket_frame["レース番号"], errors="raise"
        ).astype("int64")

    if not horse_frame.empty:
        horse_frame = horse_frame.sort_values(
            ["日付", "開催", "R", "馬番"], kind="stable"
        ).reset_index(drop=True)
    if not ticket_frame.empty:
        ticket_frame = ticket_frame.sort_values(
            ["日付", "競馬場名", "レース番号", "券種"], kind="stable"
        ).reset_index(drop=True)
    return horse_frame, ticket_frame
