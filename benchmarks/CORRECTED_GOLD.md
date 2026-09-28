# The corrected gold set, and why it is pinned

Every "corrected gold" number in this repository is scored against a
re-annotation of BIRD mini-dev, not against BIRD's own labels. BIRD's labels
have a **52.8% error rate** (VLDB 2026, uiuc-kang-lab), so the original gold is
a noisy target and a system can be marked wrong for producing the right answer.

## The file

| | |
|---|---|
| repository | `https://github.com/uiuc-kang-lab/ReViSQL` |
| commit | `9fac371aa22019e9912dcbd6572e8fe8194d352a` (2026-07-18) |
| file | `data/arcwise_plat_sql.json` — 498 entries |
| sha256 | `5927cd9320546ee02c0540c6299f3ff068709efbcbabf87d81a74f85749b5ce2` |

`arcwise_plat_sql.json` corrects **only the SQL**. `arcwise_plat_full.json`
(sha256 `d16716e3...`) also rewrites questions and external knowledge, which
changes what was asked — use the SQL-only set unless you mean to change the
questions too.

The repository has no licence file, so the data is **not vendored here**. Fetch
it:

```sh
git clone --depth 1 https://github.com/uiuc-kang-lab/ReViSQL.git
sha256sum ReViSQL/data/arcwise_plat_sql.json   # must match the hash above
```

## Why this file is pinned rather than just named

A previous corrected set — `arcwise_plat_sql_only_with_diff.json`, obtained
from somewhere no longer recorded — lived in a temporary directory. The
directory was cleared mid-session and the file went with it. Every corrected
number measured against it became unreproducible in one step.

Worse, it was not equivalent. Re-scoring the same run against the canonical
file above moved `fast` from 70.9% (295/416) to **72.4% (302/417)**, and moved
a p-value in the intent-check A/B from 0.021 to 0.077 — from "significant" to
"not". One unpinned dependency changed a conclusion.

`rescore.py` now prints the filename and sha256 of the corrected set with every
score. A corrected number quoted without that pair is not a measurement.
