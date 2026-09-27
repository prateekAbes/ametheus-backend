import pytest
from tests.conftest import make_auth_headers


@pytest.mark.asyncio
async def test_list_and_seed_regulations(client, db_session):
    # 1. Public list should return seeded regulations
    res = await client.get("/api/v1/regulations")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 5
    # Verify initial games present
    game_names = [d["game_name"] for d in data]
    assert "BGMI" in game_names
    assert "VALORANT" in game_names
    assert "Free Fire MAX" in game_names
    assert "Call of Duty: Mobile" in game_names
    assert "Counter-Strike 2" in game_names

    # Check chapter codes are present and dynamic
    chapter_codes = [d["chapter_code"] for d in data]
    assert "BGMI-01" in chapter_codes
    assert "VAL-01" in chapter_codes


@pytest.mark.asyncio
async def test_create_edit_reorder_delete_regulation(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")

    # 1. Admin creates a 6th game regulation (e.g. Apex Legends)
    payload = {
        "game_name": "Apex Legends",
        "chapter_code": "APL-01",
        "title": "Apex Legends (Trios BR)",
        "format": "Trios Battle Royale | ALGS 20-Point Matrix",
        "server": "Singapore / Bahrain AWS Servers",
        "map_pool": ["World's Edge", "Storm Point", "Olympus"],
        "rules": [
            "Roster Size: Exactly 3 players.",
            "Scoring: Official ALGS scoring matrix.",
        ],
        "order": 6,
    }
    create_res = await client.post("/api/v1/regulations", json=payload, headers=admin_headers)
    assert create_res.status_code == 201
    created_data = create_res.json()
    reg_id = created_data["id"]
    assert created_data["chapter_code"] == "APL-01"
    assert created_data["game_name"] == "Apex Legends"

    # 2. Public endpoint should now return 6+ games
    list_res = await client.get("/api/v1/regulations")
    assert list_res.status_code == 200
    assert any(r["id"] == reg_id for r in list_res.json())

    # 3. Admin edits regulation (change chapter code to APL-02)
    update_res = await client.put(
        f"/api/v1/regulations/{reg_id}",
        json={"chapter_code": "APL-02", "format": "Updated Format"},
        headers=admin_headers,
    )
    assert update_res.status_code == 200
    assert update_res.json()["chapter_code"] == "APL-02"
    assert update_res.json()["format"] == "Updated Format"

    # 4. Admin reorders regulations
    reorder_res = await client.post(
        "/api/v1/regulations/reorder",
        json={"ordered_ids": [reg_id]},
        headers=admin_headers,
    )
    assert reorder_res.status_code == 200

    # 5. Admin deletes the test regulation
    del_res = await client.delete(f"/api/v1/regulations/{reg_id}", headers=admin_headers)
    assert del_res.status_code == 200


@pytest.mark.asyncio
async def test_regulation_table_builder(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")

    # 1. List seeded table
    res = await client.get("/api/v1/regulations/tables")
    assert res.status_code == 200
    tables = res.json()
    assert len(tables) >= 1
    assert "columns" in tables[0]
    assert "rows" in tables[0]

    # 2. Create custom table with arbitrary columns & rows
    tbl_payload = {
        "title": "Custom Tournament Discipline Matrix",
        "description": "Custom table with dynamic columns",
        "columns": ["Game", "Chapter Code", "Server", "Slots"],
        "rows": [
            ["Rocket League", "RL-01", "India Dedicated", "32 Teams"],
            ["Rainbow Six Siege", "R6S-01", "AWS Mumbai", "16 Teams"],
        ],
        "order": 2,
    }
    create_tbl_res = await client.post("/api/v1/regulations/tables", json=tbl_payload, headers=admin_headers)
    assert create_tbl_res.status_code == 201
    tbl_data = create_tbl_res.json()
    tbl_id = tbl_data["id"]
    assert len(tbl_data["rows"]) == 2
    assert len(tbl_data["columns"]) == 4

    # 3. Update table (add another row)
    update_tbl_res = await client.put(
        f"/api/v1/regulations/tables/{tbl_id}",
        json={
            "rows": [
                ["Rocket League", "RL-01", "India Dedicated", "32 Teams"],
                ["Rainbow Six Siege", "R6S-01", "AWS Mumbai", "16 Teams"],
                ["Overwatch 2", "OW-01", "Asia Pacific", "16 Teams"],
            ]
        },
        headers=admin_headers,
    )
    assert update_tbl_res.status_code == 200
    assert len(update_tbl_res.json()["rows"]) == 3

    # 4. Clean up test table
    del_tbl_res = await client.delete(f"/api/v1/regulations/tables/{tbl_id}", headers=admin_headers)
    assert del_tbl_res.status_code == 200
