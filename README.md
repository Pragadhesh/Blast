# Blast

**Know what breaks before you merge.**

Blast is a GitHub Action + AI agent that reads a pull request that changes a
dbt model or schema file, queries [DataHub](https://datahubproject.io/) —
through its **MCP Server** — for everything downstream, **simulates whether
that downstream code would actually break**, and posts a risk report as a PR
comment before anyone merges — then writes the finding back into DataHub as
an incident, so the next PR against that table inherits the history.

Comment `/blast-fix` on a reported PR and **Splint**, a second agent, will
generate a corrected version of the broken downstream code and open it as a
follow-up PR for review — turning the warning into an actual fix.

Built for the DataHub hackathon, targeting the **Agents That Do Real Work**
and **Metadata-Aware Code Generation & Development** tracks.


## Quickstart — run the bundled demo (no infra required)

This reproduces `examples/sample_pr_comment.md` exactly, using a mock
DataHub lineage fixture and a mock (network-free) LLM summarizer:

```bash
pip install -r requirements.txt
python agents/blast-scan/main.py --demo
```

That's it — no Docker, no OpenAI key, no GitHub token needed. The demo diffs
`examples/demo_dbt_project` (baseline) against `examples/demo_pr_change`
(proposed PR) for a renamed + retyped column in `stg_orders`, and prints the
full PR comment — findings table, Mermaid dependency graph, incident-history
line, and summary — to stdout. See
`examples/demo_pr_change/CHANGE_SCENARIO.md` for the exact change and the
expected classification for each downstream model.

## Running against a real DataHub instance + real PR

1. **Start DataHub locally** (needs Docker Desktop, 8GB+ RAM allocated):

   ```bash
   python3 -m pip install --upgrade pip wheel setuptools
   python3 -m pip install --upgrade acryl-datahub
   datahub docker quickstart
   datahub init
   datahub datapack load showcase-ecommerce   # or ingest your own dbt project's manifest
   ```

   UI at http://localhost:9002 (login `datahub`/`datahub`). Generate a
   personal access token under **Settings → Access Tokens**.

2. **Copy `.env.example` to `.env`** and fill in `OPENAI_API_KEY`,
   `DATAHUB_SERVER`, `DATAHUB_TOKEN`. Set `BLAST_MOCK_DATAHUB=0` and
   `BLAST_MOCK_LLM=0` to use the real services. Lineage reads try DataHub's
   MCP Server first and fall back to GraphQL automatically
   (`BLAST_DATAHUB_MODE=auto`) — see `docs/architecture.md`'s known
   limitations for what's verified vs. not about that path.

3. **Install dependencies**: `pip install -r requirements.txt`

4. **Adopt the reusable workflows in your own repo** — no vendored Python,
   just two 5-line wrapper files. Copy
   `examples/consumer-workflows/blast-scan.yml` and
   (optionally) `examples/consumer-workflows/blast-fix.yml` into your repo's
   `.github/workflows/`, then add `OPENAI_API_KEY`, `DATAHUB_SERVER`,
   `DATAHUB_TOKEN` as [repository secrets](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions).
   This only works once Blast itself is pushed to a real GitHub remote that
   `uses:` can point at.

5. Open a PR against your dbt project that changes a model or its
   `schema.yml` — Blast comments automatically. Comment `/blast-fix` on it
   to have Splint propose a fix.

## How it works

See [`docs/architecture.md`](docs/architecture.md) for the full pipeline
diagrams (blast-scan and Splint) and module-by-module design notes,
including known limitations.

## Repo layout

```
blast/
├── LICENSE                       # Apache 2.0
├── agents/
│   ├── _common/                  # shared by both agents: diff parsing, DataHub client
│   │                              # (MCP-first, GraphQL fallback), breakage classification
│   ├── blast-scan/                # reads a PR, reports risk
│   └── splint/                    # /blast-fix: proposes a fix as a follow-up PR
├── .github/workflows/
│   ├── blast-scan.yml             # reusable workflow, called from any repo
│   └── blast-fix.yml              # reusable workflow, triggers Splint
├── examples/
│   ├── sample_pr_comment.md       # real output from `python agents/blast-scan/main.py --demo`
│   ├── demo_dbt_project/          # baseline demo dbt project + mock lineage fixture
│   ├── demo_pr_change/            # proposed PR version of the changed files
│   └── consumer-workflows/        # copy-paste wrapper workflows for adopting repos
└── docs/architecture.md
```

## Tech stack

DataHub Core (self-hosted, Apache 2.0) · DataHub MCP Server (lineage reads,
with a GraphQL fallback) · Python · sqlglot · OpenAI `gpt-4o-mini` ·
GitHub Actions (reusable workflows) · Mermaid · PyGithub.

Everything runs for $0: DataHub Core is free and self-hosted, GitHub Actions
is free on public repos, and the only paid API (OpenAI) uses the cheapest
current model and is fully mockable during development (`BLAST_MOCK_LLM=1`).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).