"""Retrieving worked examples for the question being asked.

Removing dynamic few-shot exemplars is the largest single delta in the only
fine-grained ablation published on BIRD mini-dev: -6.2 at generation, against
-4.2 for schema extraction and -1.4 for value retrieval. This pipeline
answered every question zero-shot.
"""

from __future__ import annotations

import json

from sqlagent.exemplars import Exemplar, ExemplarIndex, load, render


def _index():
    return ExemplarIndex(
        [
            Exemplar("How many schools are in Alameda county?",
                     "SELECT count(*) FROM schools WHERE county = 'Alameda'"),
            Exemplar("What is the average math score per district?",
                     "SELECT district, avg(math) FROM scores GROUP BY district"),
            Exemplar("List the top 5 drivers by points.",
                     "SELECT name FROM drivers ORDER BY points DESC LIMIT 5"),
        ]
    )


def test_the_most_similar_question_comes_first():
    top = _index().retrieve("How many hospitals are in Marin county?", k=1)

    assert top[0].question.startswith("How many schools")


def test_a_ranking_question_retrieves_the_ranking_example():
    """Not necessarily first — "score" also matches the averaging example,
    and BM25 is entitled to rank on term rarity rather than on the intent a
    human reads into the sentence. What matters is that it is retrieved."""
    got = _index().retrieve("Give me the top 3 teams by score.", k=3)

    assert any("ORDER BY" in e.sql for e in got)


def test_only_examples_that_share_a_term_are_returned():
    """Padding the list out to k with unrelated examples would spend tokens
    teaching the model a form that has nothing to do with the question."""
    assert len(_index().retrieve("schools county average", k=2)) == 2
    # The third example shares no content word, so k=99 still returns two.
    assert len(_index().retrieve("schools county average", k=99)) == 2
    assert _index().retrieve("anything", k=0) == []


def test_a_question_sharing_only_stopwords_retrieves_nothing():
    """Every question contains "how many" and "what is". Matching on those
    would return the same three examples for every question ever asked."""
    assert _index().retrieve("what is the how of it", k=3) == []


def test_an_empty_index_is_not_an_error():
    assert ExemplarIndex([]).retrieve("anything", k=3) == []
    assert render([]) == ""


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_bird_verified_fields_are_read(tmp_path):
    """BIRD-Verified keeps the corrected question and SQL under the plain
    names and the pre-correction ones under `original_*`. The corrected pair
    is the one worth showing a model."""
    path = tmp_path / "ex.json"
    path.write_text(json.dumps([{
        "question": "corrected question",
        "original_question": "the wrong one",
        "SQL": "SELECT 1",
        "original_SQL": "SELECT 2",
        "evidence": "a hint",
    }]))

    index = load(path)

    assert len(index) == 1
    assert index.exemplars[0].question == "corrected question"
    assert index.exemplars[0].sql == "SELECT 1"


def test_an_overlong_example_is_skipped(tmp_path):
    path = tmp_path / "ex.json"
    path.write_text(json.dumps([
        {"question": "short", "SQL": "SELECT 1"},
        {"question": "long", "SQL": "SELECT " + "x" * 800},
    ]))

    assert len(load(path)) == 1


def test_a_missing_file_yields_an_empty_index(tmp_path):
    """A missing example file must not stop a question being answered."""
    assert len(load(tmp_path / "nope.json")) == 0


def test_a_malformed_file_yields_an_empty_index(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")

    assert len(load(path)) == 0


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_the_block_warns_that_the_tables_are_from_elsewhere():
    """Cross-database exemplars cannot leak an answer, but they can be copied
    from if the model mistakes them for the schema."""
    text = render(_index().retrieve("How many schools?", k=1))

    assert "must not be used" in text
    assert "SELECT count(*)" in text


def test_the_evidence_hint_travels_with_the_example():
    text = render([Exemplar("q", "SELECT 1", evidence="ratio means a/b")])

    assert "ratio means a/b" in text
