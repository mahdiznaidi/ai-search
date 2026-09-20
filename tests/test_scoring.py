import json

import pytest

from stagefinder import db, scoring as S
from stagefinder.dedup import upsert_job
from stagefinder.models import Fact, RawJob, add_terms, create_campaign, load_campaign

FACTS = [Fact(1, "skill", "Python", "", ["Python"], "", "")]


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, titles, threshold=70):
    cid = create_campaign(conn, "c", "stage data science", "FR", score_threshold=threshold)
    ids = []
    for i, t in enumerate(titles):
        r = upsert_job(conn, RawJob("indeed", f"k{i}", t, f"Co{i}", "Paris",
                                    f"https://x/{i}", "d"), cid, "k", "l")
        conn.execute("UPDATE jobs SET status='enriched', full_description=?, "
                     "enriched=? WHERE id=?",
                     (f"{t} python données", json.dumps({"missions": t, "requirements": [],
                                                         "tech_stack": [], "team_context": ""}),
                      r.job_id))
        ids.append(r.job_id)
    conn.commit()
    return load_campaign(conn, cid), ids


def test_rule_prefilter():
    assert S.rule_prefilter("stage data python", ["data"], ["senior"])
    assert not S.rule_prefilter("stage data senior", ["data"], ["senior"])
    assert not S.rule_prefilter("stage rh", ["data"], [])
    assert S.rule_prefilter("n'importe quoi", [], [])  # sans must_have : tout passe


def test_score_pending_writes_scores_and_statuses(conn, fake_llm):
    camp, ids = _seed(conn, ["Stage Data Scientist", "Stage Data Analyst", "Stage RH"])
    add_terms(conn, camp.id, "exclude", ["RH"])
    fake_llm.responses = [json.dumps([
        {"index": 0, "score": 85, "reasons": ["ML", "Python"]},
        {"index": 1, "score": 40, "reasons": ["peu de ML"]},
    ])]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c == {"scored": 1, "skipped": 2, "failed": 0}
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM jobs")}
    assert st[ids[0]] == "scored" and st[ids[1]] == "skipped" and st[ids[2]] == "skipped"
    sc = conn.execute("SELECT score, reasons FROM scores WHERE job_id=?", (ids[0],)).fetchone()
    assert sc["score"] == 85 and json.loads(sc["reasons"]) == ["ML", "Python"]
    assert "stage data science" in fake_llm.prompts[0][0]  # l'intent pilote le prompt


def test_missing_index_in_llm_answer_is_failed_not_kept(conn, fake_llm):
    camp, ids = _seed(conn, ["A", "B"])
    fake_llm.responses = [json.dumps([{"index": 0, "score": 90, "reasons": []}])]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c["scored"] == 1 and c["failed"] == 1
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (ids[1],)).fetchone()
    assert row["status"] == "failed_score" and row["failure_reason"]


def test_llm_error_marks_batch_failed(conn, fake_llm):
    from stagefinder.llm import LLMError
    camp, ids = _seed(conn, ["A"])

    def boom():
        raise LLMError("x")
    fake_llm.responses = [boom]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c == {"scored": 0, "skipped": 0, "failed": 1}


def test_batches(conn, fake_llm):
    camp, ids = _seed(conn, [f"T{i}" for i in range(7)])
    fake_llm.responses = [
        json.dumps([{"index": i, "score": 80, "reasons": []} for i in range(5)]),
        json.dumps([{"index": i, "score": 80, "reasons": []} for i in range(2)]),
    ]
    assert S.score_pending(conn, fake_llm, camp, FACTS, batch_size=5)["scored"] == 7
    assert fake_llm.calls == 2


def test_non_list_llm_answer_is_failed_score(conn, fake_llm):
    camp, ids = _seed(conn, ["A"])
    fake_llm.responses = ['{"index": 0, "score": 90}']
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c == {"scored": 0, "skipped": 0, "failed": 1}
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "failed_score" and row["failure_reason"]
