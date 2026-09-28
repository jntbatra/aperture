"""Dependency-light DB value index: stdlib sqlite3 only. FTS5 trigram + difflib rerank."""
import sqlite3, os, re, time, difflib, unicodedata

SKIP_TOK = ("_id", " id", "url", "email", "web", "time", "phone", "date", "address")

def _norm(s: str) -> str:
    # strip emoji/symbols/punct decoration, collapse space, casefold
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith(("So", "Sk", "Cn", "Mn")))
    return re.sub(r"\s+", " ", s).strip().casefold()

def build(db_path: str, index_path: str, max_distinct=20000, max_len=64):
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    src.text_factory = lambda b: b.decode("utf8", "replace")
    ix = sqlite3.connect(index_path)
    ix.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE IF NOT EXISTS val(id INTEGER PRIMARY KEY, tbl TEXT, col TEXT, raw TEXT, norm TEXT);
      CREATE VIRTUAL TABLE IF NOT EXISTS val_fts USING fts5(norm, content='val', content_rowid='id', tokenize='trigram');
    """)
    cur = src.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'") if r[0] != "sqlite_sequence"]
    pks, n = set(), 0
    for t in tables:
        for c in cur.execute(f'PRAGMA table_info("{t}")'):
            if c[5] > 0: pks.add(c[1].lower())
    for t in tables:
        for c in cur.execute(f'PRAGMA table_info("{t}")').fetchall():
            name, ctype = c[1], (c[2] or "").upper()
            low = name.lower()
            if "CHAR" not in ctype and "TEXT" not in ctype and "CLOB" not in ctype: continue
            if low in pks or any(k in low for k in SKIP_TOK) or name.endswith("Id"): continue
            try:
                vals = [r[0] for r in cur.execute(
                    f'SELECT DISTINCT "{name}" FROM "{t}" WHERE "{name}" IS NOT NULL '
                    f'AND LENGTH("{name}") BETWEEN 1 AND {max_len} LIMIT {max_distinct+1}')]
            except sqlite3.Error:
                continue
            if len(vals) > max_distinct: continue      # free-text column: skip
            ix.executemany("INSERT INTO val(tbl,col,raw,norm) VALUES(?,?,?,?)",
                           [(t, name, str(v), _norm(str(v))) for v in vals])
            n += len(vals)
    ix.execute("INSERT INTO val_fts(rowid,norm) SELECT id,norm FROM val")
    ix.execute("INSERT INTO val_fts(val_fts) VALUES('optimize')")
    ix.commit(); ix.close(); src.close()
    return n

SQL = ("SELECT v.tbl,v.col,v.raw,v.norm FROM val_fts f JOIN val v ON v.id=f.rowid "
       "WHERE val_fts MATCH ? LIMIT 200")

def _probes(q):
    """FTS5 trigram MATCH is *substring containment*: it finds values containing the
    probe. So also probe shortened forms, or 'members' never matches stored 'Member'."""
    seen, out = set(), []
    def add(p):
        p = p.strip()
        if len(p) >= 3 and p not in seen:
            seen.add(p); out.append(p)
    add(q)
    for t in sorted(q.split(), key=len, reverse=True)[:3]:
        add(t)
        if t.endswith("es"): add(t[:-2])
        if t.endswith("s"):  add(t[:-1])
        if len(t) >= 6:      add(t[:max(4, len(t) - 2)])   # crude stem
    return out

def search(index_path, keyword, top_n=5, min_sim=0.55):
    q = _norm(keyword)
    if len(q) < 3: return []
    ix = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    rows = []
    for probe in _probes(q):
        try:
            rows += ix.execute(SQL, ('"' + probe.replace('"', '""') + '"',)).fetchall()
        except sqlite3.OperationalError:
            pass
        if len(rows) > 400: break
    ix.close()
    out = []
    for tbl, col, raw, nrm in rows:
        sim = difflib.SequenceMatcher(None, q, nrm).ratio()
        if q in nrm: sim = max(sim, 0.9)          # containment wins
        if sim >= min_sim: out.append((sim, tbl, col, raw))
    out.sort(reverse=True)
    seen, res = set(), []
    for sim, tbl, col, raw in out:
        if (tbl, col, raw) in seen: continue
        seen.add((tbl, col, raw)); res.append((round(sim, 3), tbl, col, raw))
        if len(res) >= top_n: break
    return res
