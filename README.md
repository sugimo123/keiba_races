# keiba_races
Location of race analysis results

## Extracting race history

Install the Python dependencies with `python -m pip install -r requirements.txt`,
then run this from the repository root:

```python
from src.keiba_history import load_history

horse_df, ticket_df = load_history(".")
```

`horse_df` contains every column in each committed `docs/index.html` race
table, plus the race date. `ticket_df` contains one row per recommended ticket,
with horse numbers represented as tuples. If a race appears in multiple
commits, its newest reachable snapshot is used. Reading history does not
checkout commits or modify the worktree. Legacy numeric race headings use the
commit author's local date, and legacy odds columns named `単` are exposed as
`単勝`.

Install the test dependency with `python -m pip install -r requirements-dev.txt`,
then run the tests with `python -m pytest`.
