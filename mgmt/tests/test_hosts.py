from __future__ import annotations

from helpers import auth_header


def _get_join_token(client, admin_token) -> str:
    resp = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _enroll_body(name="node-a", address="10.0.1.11:8443", trust_token="good-token"):
    return {
        "name": name,
        "address": address,
        "project": "folia",
        "lxd_trust_token": trust_token,
        "capacity": {"cpu_cores": 6, "memory_gb": 12},
        "labels": {"cpu_type": "p-core"},
    }


def test_enroll_happy_path(client, admin_token, fake_lxd):
    join_token = _get_join_token(client, admin_token)
    resp = client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "node-a"
    assert body["status"] == "online"
    assert body["allocated_cpu_cores"] == 0


def test_enroll_rejects_bad_join_token(client):
    resp = client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header("not-a-real-token"))
    assert resp.status_code == 401


def test_join_token_is_single_use(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    first = client.post("/api/v1/hosts/enroll", json=_enroll_body(name="node-a"), headers=auth_header(join_token))
    assert first.status_code == 200

    second = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(name="node-b", address="10.0.1.12:8443"),
        headers=auth_header(join_token),
    )
    assert second.status_code == 401


def test_enroll_rejects_duplicate_host_name(client, admin_token):
    join_token_1 = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(name="node-a"), headers=auth_header(join_token_1))

    join_token_2 = _get_join_token(client, admin_token)
    resp = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(name="node-a", address="10.0.1.99:8443"),
        headers=auth_header(join_token_2),
    )
    assert resp.status_code == 409


def test_enroll_surfaces_lxd_trust_failure(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    resp = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(trust_token="bad-token"),
        headers=auth_header(join_token),
    )
    assert resp.status_code == 502


def test_list_hosts_requires_auth(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.get("/api/v1/hosts", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_drain_host(client, admin_token, operator_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.post("/api/v1/hosts/node-a/drain", headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "draining"


def test_set_domains_happy_path(client, admin_token, operator_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.put(
        "/api/v1/hosts/node-a/domains",
        json={"domains": ["SMP.Example.com", "other.example.com."]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    # normalized: lowercased, trailing dot stripped
    assert resp.json()["domains"] == ["smp.example.com", "other.example.com"]


def test_set_domains_clears_with_empty_list(client, admin_token, operator_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))
    client.put(
        "/api/v1/hosts/node-a/domains",
        json={"domains": ["smp.example.com"]},
        headers=auth_header(operator_token),
    )

    resp = client.put("/api/v1/hosts/node-a/domains", json={"domains": []}, headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json()["domains"] == []


def test_set_domains_rejects_empty_string(client, admin_token, operator_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.put(
        "/api/v1/hosts/node-a/domains", json={"domains": ["  "]}, headers=auth_header(operator_token)
    )
    assert resp.status_code == 400


def test_set_domains_unknown_host(client, operator_token):
    resp = client.put(
        "/api/v1/hosts/no-such-host/domains",
        json={"domains": ["smp.example.com"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 404


def test_set_domains_rejects_conflict_with_another_host(client, admin_token, operator_token):
    join_token_1 = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(name="node-a"), headers=auth_header(join_token_1))
    join_token_2 = _get_join_token(client, admin_token)
    client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(name="node-b", address="10.0.1.12:8443"),
        headers=auth_header(join_token_2),
    )

    client.put(
        "/api/v1/hosts/node-a/domains", json={"domains": ["smp.example.com"]}, headers=auth_header(operator_token)
    )
    resp = client.put(
        "/api/v1/hosts/node-b/domains", json={"domains": ["smp.example.com"]}, headers=auth_header(operator_token)
    )
    assert resp.status_code == 409


def test_set_domains_requires_operator(client, admin_token, viewer_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.put(
        "/api/v1/hosts/node-a/domains", json={"domains": ["smp.example.com"]}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403
