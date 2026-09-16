import pytest
from app.models.match import Match
from tests.conftest import TestSessionLocal

@pytest.mark.asyncio
async def test_match_score_submission_flow(client):
    async with TestSessionLocal() as db:
        m = Match(
            tournament_id="t-val-mock",
            stage="Quarterfinals - Match 1",
            round_number=1,
            match_number=1,
            format="BO3",
            scheduled_time="Now",
            status="LIVE",
            server_region="AWS Mumbai",
            lobby_code="ROOM-99",
            lobby_password="PASS",
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        m_id = m.id

    # 1. Submit match score
    submit_payload = {
        "score_a": 13,
        "score_b": 7,
        "evidence_url": "https://r2.ametheus.gg/proof.png",
        "captain_notes": "Clean match, no overtimes",
    }
    resp = await client.post(f"/api/v1/matches/{m_id}/submit-result", json=submit_payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"

    # 2. Verify match status in list
    list_resp = await client.get("/api/v1/matches")
    assert list_resp.status_code == 200
    found = [x for x in list_resp.json() if x["id"] == m_id]
    assert len(found) == 1
    assert found[0]["status"] == "PENDING_VERIFICATION"
