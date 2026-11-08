from pathlib import Path

import pandas as pd
import pygit2
import pytest

from src.keiba_history import _parse_snapshot, load_history


def race_html(
    race_id: str = "2026-11-07_京都12",
    horse_name: str = "馬A",
    odds: str = "4.2",
    venue: str = "京都",
    race_number: int = 12,
    tickets: str = "<li>1 - 9</li><li>3</li>",
) -> bytes:
    return f"""<!DOCTYPE html>
    <html lang="ja"><body>
      <h1>{race_id}</h1>
      <table class="dataframe">
        <thead><tr>
          <th>開催</th><th>R</th><th>馬番</th><th>馬名</th><th>単勝</th>
          <th>騎手名</th><th>調教師名</th><th>勝率</th><th>残差</th>
          <th>新列</th>
        </tr></thead>
        <tbody><tr>
          <td>{venue}</td><td>{race_number}</td><td>9</td><td>{horse_name}</td><td>{odds}</td>
          <td>騎手</td><td>調教師</td><td>0.2</td><td>-0.1</td><td>情報</td>
        </tr></tbody>
      </table>
      <div class="tickets-container"><div class="bet-group">
        <h3>ワイド</h3><ul>{tickets}</ul>
      </div></div>
    </body></html>""".encode("utf-8")


def add_snapshot(
    repository: pygit2.Repository,
    html: bytes,
    parent: pygit2.Oid | None = None,
) -> pygit2.Oid:
    blob_id = repository.create_blob(html)
    docs_builder = repository.TreeBuilder()
    docs_builder.insert("index.html", blob_id, pygit2.GIT_FILEMODE_BLOB)
    docs_tree_id = docs_builder.write()

    root_builder = repository.TreeBuilder()
    root_builder.insert("docs", docs_tree_id, pygit2.GIT_FILEMODE_TREE)
    tree_id = root_builder.write()
    signature = pygit2.Signature("Test", "test@example.com", 1_800_000_000, 0)
    parents = [] if parent is None else [parent]
    return repository.create_commit(
        "HEAD", signature, signature, "Update race page", tree_id, parents
    )


def test_parse_snapshot_keeps_all_table_columns_and_tuple_tickets() -> None:
    horses, headers, tickets = _parse_snapshot(race_html())

    assert headers == [
        "開催", "R", "馬番", "馬名", "単勝", "騎手名", "調教師名", "勝率", "残差", "新列"
    ]
    assert horses[0] == {
        "日付": "2026-11-07",
        "開催": "京都",
        "R": "12",
        "馬番": "9",
        "馬名": "馬A",
        "単勝": "4.2",
        "騎手名": "騎手",
        "調教師名": "調教師",
        "勝率": "0.2",
        "残差": "-0.1",
        "新列": "情報",
    }
    assert [ticket["買い目"] for ticket in tickets] == [(1, 9), (3,)]
    assert tickets[0]["券種"] == "ワイド"


def test_parse_snapshot_normalizes_legacy_odds_header() -> None:
    legacy_html = race_html().replace(
        "<th>単勝</th>".encode("utf-8"), "<th>単</th>".encode("utf-8")
    )

    horses, headers, _ = _parse_snapshot(legacy_html)

    assert "単勝" in headers
    assert "単" not in headers
    assert horses[0]["単勝"] == "4.2"


def test_parse_snapshot_supports_legacy_race_id_heading() -> None:
    legacy_html = race_html(race_id="202607010612").replace(
        "<th>単勝</th>".encode("utf-8"), "<th>単</th>".encode("utf-8")
    )

    horses, _, tickets = _parse_snapshot(legacy_html, fallback_date="2026-03-29")

    assert horses[0]["日付"] == "2026-03-29"
    assert tickets[0]["レース番号"] == 12


def test_load_history_reads_latest_unique_race_without_checkout(tmp_path: Path) -> None:
    repository = pygit2.init_repository(str(tmp_path / "repo"), False)
    old_commit = add_snapshot(repository, race_html(horse_name="旧名", odds="3.1"))
    add_snapshot(
        repository,
        race_html(horse_name="新名", odds="5.6"),
        parent=old_commit,
    )
    add_snapshot(
        repository,
        race_html(
            race_id="2026-11-08_東京1",
            horse_name="別レース",
            venue="東京",
            race_number=1,
        ),
        parent=repository.head.target,
    )

    horses, tickets = load_history(repository.workdir)

    assert len(horses) == 2
    assert set(horses["馬名"]) == {"新名", "別レース"}
    assert "新列" in horses.columns
    assert pd.api.types.is_datetime64_any_dtype(horses["日付"])
    assert horses.loc[horses["馬名"] == "新名", "単勝"].item() == 5.6
    assert len(tickets) == 4
    assert tickets["買い目"].tolist() == [(1, 9), (3,), (1, 9), (3,)]


def test_load_history_handles_race_without_ticket_section(tmp_path: Path) -> None:
    repository = pygit2.init_repository(str(tmp_path / "repo"), False)
    add_snapshot(repository, race_html(tickets=""))

    horses, tickets = load_history(repository.workdir)

    assert len(horses) == 1
    assert tickets.empty
    assert list(tickets.columns) == ["日付", "競馬場名", "レース番号", "券種", "買い目"]


def test_parse_snapshot_rejects_malformed_tickets() -> None:
    with pytest.raises(ValueError, match="Unexpected ticket format"):
        _parse_snapshot(race_html(tickets="<li>1 / 9</li>"))
