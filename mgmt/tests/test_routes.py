from __future__ import annotations

from folia_mgmt.models import Host, World, WorldPhase, WorldType

from helpers import auth_header


def _host(name="node-a", domains=None):
    return Host(
        name=name,
        address="10.0.1.10:8443",
        capacity_cpu_cores=8,
        capacity_memory_gb=16,
        domains=domains or [],
    )


def _world(name, type_, host_name, phase=WorldPhase.running, address="10.0.1.20:25565"):
    return World(
        name=name,
        type=type_,
        cpu_cores=1,
        memory_gb=1,
        phase=phase,
        host_name=host_name,
        container_name=name,
        address=address,
    )


def test_domain_resolves_to_hosts_lobby(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=["smp.example.com"]))
    db_session.add(_world("world-lobby", WorldType.lobby, "node-a"))
    db_session.add(_world("world-overworld", WorldType.overworld, "node-a"))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["domains"] == {"smp.example.com": "world-lobby"}


def test_domain_falls_back_to_overworld_when_no_lobby(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=["smp.example.com"]))
    db_session.add(_world("world-overworld", WorldType.overworld, "node-a"))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    assert resp.json()["domains"] == {"smp.example.com": "world-overworld"}


def test_host_with_no_routable_worlds_is_omitted(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=["smp.example.com"]))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    assert resp.json()["domains"] == {}


def test_host_with_no_domains_contributes_nothing(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=[]))
    db_session.add(_world("world-lobby", WorldType.lobby, "node-a"))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    assert resp.json()["domains"] == {}


def test_two_hosts_split_correctly(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=["smp.example.com"]))
    db_session.add(_host("node-b", domains=["creative.example.com"]))
    db_session.add(_world("world-a-lobby", WorldType.lobby, "node-a"))
    db_session.add(_world("world-b-lobby", WorldType.lobby, "node-b"))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    assert resp.json()["domains"] == {
        "smp.example.com": "world-a-lobby",
        "creative.example.com": "world-b-lobby",
    }


def test_global_default_flag_unaffected_by_per_host_domains(client, viewer_token, db_session):
    db_session.add(_host("node-a", domains=["smp.example.com"]))
    db_session.add(_world("world-a-lobby", WorldType.lobby, "node-a"))
    db_session.add(_world("world-b-overworld", WorldType.overworld, "node-b"))
    db_session.commit()

    resp = client.get("/api/v1/routes", headers=auth_header(viewer_token))
    routes = {r["world"]: r for r in resp.json()["routes"]}
    assert routes["world-a-lobby"]["default"] is True
    assert routes["world-b-overworld"]["default"] is False
