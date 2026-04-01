from __future__ import annotations


def test_scenario_can_attach_material_after_creation(client):
    role_resp = client.post(
        "/roles",
        json={
            "name": "Backend",
            "slug": "backend-materials",
            "description": "role for materials binding",
        },
    )
    assert role_resp.status_code == 201
    role_id = role_resp.json()["id"]

    scenario_resp = client.post(
        "/scenarios",
        json={
            "role_id": role_id,
            "name": "HTTP Theory",
            "slug": "http-theory",
            "description": "scenario without material",
            "difficulty": "middle",
            "tasks": [
                {
                    "id": "T-HTTP",
                    "type": "theory",
                    "title": "HTTP basics",
                }
            ],
        },
    )
    assert scenario_resp.status_code == 201
    scenario_id = scenario_resp.json()["id"]
    assert scenario_resp.json()["rag_corpus_id"] is None

    corpus_resp = client.post(
        "/rag/corpora",
        json={
            "name": "HTTP Handbook",
            "description": "material for http theory",
        },
    )
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    update_resp = client.put(
        f"/scenarios/{scenario_id}",
        json={"rag_corpus_id": corpus_id},
    )
    assert update_resp.status_code == 200
    payload = update_resp.json()
    assert payload["id"] == scenario_id
    assert payload["rag_corpus_id"] == corpus_id
