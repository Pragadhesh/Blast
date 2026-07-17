# Blast Architecture

Two agents, sharing one library.

```
agents/
├── _common/              # shared by both agents
│   ├── diff_parser.py
│   ├── sql_utils.py
│   ├── datahub_client.py      # MCP-first lineage reads, GraphQL fallback, incident read/write
│   ├── mcp_lineage_client.py  # real DataHub MCP Server client
│   ├── impact_simulator.py    # sqlglot-based breakage classification
│   └── github_repo_client.py  # branch / commit / PR helpers (Splint only)
├── blast-scan/            # reads a PR, reports risk
│   ├── main.py
│   ├── report_generator.py
│   ├── graph_renderer.py
│   └── github_commenter.py
└── splint/                 # /blast-fix: proposes a fix as a follow-up PR
    ├── main.py
    ├── fix_generator.py
    └── pr_writer.py
```

## blast-scan pipeline

```
GitHub PR (schema/dbt change)
        │
        ▼
.github/workflows/blast-scan.yml ──► agents/blast-scan/main.py
        │
        ├─ 1. diff_parser.py       old vs new model/schema.yml → SchemaDiff (per model)
        ├─ 2. datahub_client.py    downstream lineage for the changed dataset (MCP-first, GraphQL fallback)
        ├─ 3. impact_simulator.py  sqlglot classification: 🔴 hard break / 🟡 silent risk / 🟢 safe
        ├─ 4. datahub_client.py    count prior Blast incidents on the dataset (90-day window)
        ├─ 5. report_generator.py  plain-English summary (gpt-4o-mini, mockable)
        ├─ 6. graph_renderer.py    color-coded Mermaid graph
        ├─ 7. github_commenter.py  post/update the PR comment (includes the incident-history line)
        └─ 8. datahub_client.py    write an Incident back onto the changed dataset
```

`agents/blast-scan/main.py` is the only entrypoint; every other module is a
pure function/class with no I/O side effects beyond its own documented job.

## Splint pipeline (`/blast-fix`)

```
PR comment "/blast-fix"
        │
        ▼
.github/workflows/blast-fix.yml ──► agents/splint/main.py
        │
        ├─ 1-3. Same diff → lineage → classify steps as blast-scan (re-run fresh,
        │        not a re-parse of Blast's own past comment -- more reliable)
        ├─ 4. github_repo_client.py  fetch each broken model's REAL source file
        │       (not DataHub's compiled view SQL -- that has no Jinja and would
        │        corrupt the dbt model if written back verbatim)
        ├─ 5. fix_generator.py       LLM rewrites the SQL given the specific
        │       ColumnChange(s) and classification reason(s)
        ├─ 6. pr_writer.py           branch off the original PR's branch, commit
        │       each fix, open a follow-up PR targeting it
        ├─ 7. Comment back on the original PR linking the fix PR
        └─ 8. datahub_client.py      write a note back to DataHub (custom_type
                BLAST_FIX_PROPOSED) so the fix attempt is part of the dataset's history too
```

Splint locates a downstream model's source file by the same `models/**/{name}.sql`
convention the rest of Blast assumes (DataHub's lineage graph doesn't expose
source file paths) -- see "Known limitations" below.

## Module responsibilities

- **`diff_parser.py`** — turns a before/after pair of file contents into a
  `SchemaDiff` (a list of `ColumnChange`: `dropped` / `added` / `renamed` /
  `type_changed`). Two independent code paths feed the same `SchemaDiff` shape:
  - `.sql` model bodies, via `sqlglot` — parses the `SELECT` list of both
    versions and diffs the output column names. A `renamed` change is detected
    when a newly-added column's expression is a bare reference to a
    newly-dropped column (`order_total as total_amount` → alias detection).
  - `schema.yml` docs, via `PyYAML` — diffs each model's declared
    `name`/`data_type` pairs. Since dbt's schema.yml has no `renamed_from`
    field, a drop+add pair with an *identical* declared type is heuristically
    treated as a rename rather than two independent changes.
  - `merge_diffs()` combines diffs from both paths so a single PR that touches
    both a model's `.sql` and its `schema.yml` produces one `SchemaDiff` per
    model, not two.
  - dbt's Jinja macros (`{{ ref(...) }}`, `{{ source(...) }}`) aren't valid SQL,
    so `sql_utils.render_jinja_refs()` swaps them for their bare table name
    before handing the text to `sqlglot`. Diffing only needs the `SELECT`
    column list, not `FROM`-clause resolution, so this is enough — no real
    dbt/Jinja rendering environment is required.

- **`datahub_client.py`** — reads lineage via DataHub's **MCP Server** first
  (`mcp_lineage_client.py`, using the real `mcp` Python SDK), falling back
  automatically to a hand-rolled GraphQL client if the MCP path is
  unavailable or its result can't be confidently parsed (`BLAST_DATAHUB_MODE`
  controls this — see "Known limitations" for what's verified vs. not).
  `count_recent_incidents()` queries the dataset's existing DataHub Incidents
  and counts how many Blast raised in the last 90 days — the number behind
  the "this table has broken N times in 90 days" line in the PR comment.
  `write_incident()` calls DataHub's `raiseIncident` mutation to persist
  Blast's findings as a native DataHub Incident on the changed dataset — this
  is the "institutional memory" differentiator (CLAUDE.md §4.2): the *next*
  PR against that table inherits this history natively in DataHub's UI, not
  just in a GitHub comment that scrolls away. Splint reuses the same method
  with `custom_type="BLAST_FIX_PROPOSED"` to record fix attempts too.
  Set `BLAST_MOCK_DATAHUB=1` to read `examples/demo_dbt_project/lineage_fixture.json`
  instead of hitting a live server — this is what the demo and local dev run
  against, and what lets a judge reproduce Blast's output with zero
  infrastructure.

- **`impact_simulator.py`** — the actual simulation, shared by both agents.
  For each downstream asset's SQL, it collects every referenced column (via
  `sqlglot`'s AST, not string matching) and checks it against the diff:
  - a reference to a `dropped`/`renamed` column → **hard break** (the column
    literally won't exist at query time);
  - a reference to a `type_changed` column *inside* an aggregate or
    arithmetic expression (`SUM`, `AVG`, `MIN`, `MAX`, `+ - * /`) → **silent
    risk** — the query still runs, but its result can silently change (e.g.
    a `numeric` truncated to `integer` inside a `SUM`);
  - a reference to a `type_changed` column anywhere else → still **silent
    risk**, with a softer message (formatting/precision may differ for
    consumers, but no arithmetic is at stake);
  - no reference at all → **safe**.

  Because this operates on column names (not table-qualified paths), a
  multi-hop downstream model that references the same original column name
  through an intermediate view is classified correctly without any explicit
  transitive-propagation logic — see `examples/demo_pr_change/CHANGE_SCENARIO.md`
  for a worked 2-hop example.

- **`report_generator.py`** — provider-agnostic `Summarizer`. `MockSummarizer`
  is deterministic and network-free (used whenever `BLAST_MOCK_LLM=1`, which
  is the default in `.env.example` for local dev). `OpenAISummarizer` calls
  `gpt-4o-mini` — intentionally not a larger model, to keep the whole project
  running at effectively $0. `OllamaSummarizer` is a local fallback
  (`BLAST_LLM_PROVIDER=ollama`) if OpenAI quota becomes a risk.

- **`graph_renderer.py`** — renders the classification results as a
  color-coded Mermaid `graph LR`, which GitHub renders natively in PR comments
  with no image hosting needed.

- **`github_commenter.py`** — builds the comment body and posts/updates it via
  `PyGithub`. Each changed model gets its own hidden marker
  (`<!-- blast-report:{model} -->`) so re-runs on the same PR edit the
  existing comment instead of stacking new ones. Surfaces the incident-history
  count and, when there's a break, a pointer to `/blast-fix`.

- **`fix_generator.py`** (Splint only) — same pluggable-provider pattern as
  `report_generator.py`. `MockFixGenerator` is a deterministic regex rename
  (only handles the `renamed` case — enough to demo/test without an LLM, but
  intentionally can't fix a retype, since that needs real judgment).
  `OpenAIFixGenerator` calls `gpt-4o-mini` and is told explicitly not to
  touch Jinja macros.

- **`pr_writer.py`** (Splint only) — locates each broken model's source file
  by convention, commits the fix to a new branch off the original PR's
  branch, and opens a follow-up PR targeting it (review-before-merge, not a
  direct push to someone else's branch).

## Known limitations (by design, for hackathon scope)

- **MCP path is unverified against a live server.** `mcp_lineage_client.py`
  is a genuine, protocol-correct MCP client (real `mcp` SDK, dynamic tool
  discovery by name rather than a hardcoded assumed tool name), but this
  environment doesn't have a confirmed DataHub MCP server binary to test it
  against end-to-end. The GraphQL fallback path is what's actually verified
  (tested live against a running DataHub instance). Run with
  `BLAST_DATAHUB_MODE=mcp` to confirm your specific server/tool names work,
  rather than relying on `auto` to silently fall back.
- **Agent Context Kit / Analytics Agent are not wired up.** The hackathon
  pitch mentions usage-based prioritization via DataHub's Analytics Agent;
  this isn't implemented — MCP Server is the one new DataHub-native
  integration built for real in this pass. Roadmap item, not a claim.
- Rename detection in `schema.yml` is a heuristic (same declared type ⇒
  probably a rename), since dbt doesn't record renames explicitly. The `.sql`
  path is exact (alias-based), which is why the demo PR changes both files.
- Classification is column-reference-based static analysis, not query
  execution — it cannot catch every possible silent behavior change (e.g. a
  `CASE` expression whose branches depend on a changed column's exact values),
  only ones visible from how the column is referenced. A related gap found
  during live testing: a compiled Postgres view can expand `select *` into an
  explicit column list inside an unused CTE, which the column scanner
  currently counts as a real reference even when it never reaches the final
  output — a precision gap, not a crash, tracked as a follow-up.
- **Splint locates source files by naming convention** (`models/**/{name}.sql`),
  since DataHub's lineage graph doesn't expose a downstream asset's source
  file path. If a project doesn't follow that convention, Splint can't find
  the file to fix and skips it (logged, not silently wrong).
- **Splint on fork PRs**: comment-triggered workflows get a read-only
  `GITHUB_TOKEN` when the triggering PR is from a fork — a GitHub platform
  restriction, not something `blast-fix.yml` can route around.
