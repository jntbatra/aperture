# Aperture client libraries

Ask a database a question in English from your own code and get back the answer,
the SQL that produced it, and the rows.

| Language | Install | Docs |
|---|---|---|
| Python 3.9+ | `pip install aperture-sql` | [python/README.md](python/README.md) |
| TypeScript / JavaScript (Node 18+) | `npm install aperture-sql` | [typescript/README.md](typescript/README.md) |

Both talk to an Aperture server that you run. Setup is in
[docs/self-hosting.md](../docs/self-hosting.md), and the routes they call are in
[docs/api.md](../docs/api.md). Authenticate with an API key from the server's
`SQLAGENT_API_KEYS`.

## Tiers

Set per question. Measured on all 500 BIRD mini-dev questions against corrected
gold ([docs/10-the-ablation.md](../docs/10-the-ablation.md)), same model
(`google.gemma-4-31b`) in both:

| | `cheap` | `expensive` |
|---|---|---|
| Accuracy | 72.4% (8 runs) | 76.2% (3 runs) |
| Model calls / question | ~2.0 | ~3.1 |
| Tokens / question | ~3,000 | ~8,100 |
| Cost / question (standard) | ~$0.00045 | ~$0.00117 |
| Time / question | ~3-4s | ~5-6s |

`expensive` adds the result-intent check: after the query runs, a model reads the
rows and asks whether they answer the question, and the query is rewritten once if
not. **These numbers are from BIRD, not your database.** A larger schema sends
larger prompts, so token counts will be higher there.

Use `cheap` for dashboards and anything high-volume, `expensive` when someone is
going to act on the number.
