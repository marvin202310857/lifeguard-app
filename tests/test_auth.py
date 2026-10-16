import hashlib

from lifeguard import db
from lifeguard import settings as cfg
from tests.conftest import login


def test_first_run_creates_admin(client):
    assert login(client, "admin", "AdminPass123!").get_json()["success"]


def test_register_then_approve_then_login(client, admin):
    r = client.post("/api/register", json={"username": "swimmer1", "password": "longenough1", "role": "User"})
    assert r.get_json()["success"]

    other = client.application.test_client()
    pending = login(other, "swimmer1", "longenough1")
    assert pending.status_code == 403

    uid = next(u["id"] for u in admin.get("/api/users").get_json() if u["username"] == "swimmer1")
    assert admin.post("/api/users/approve", json={"userId": uid}).get_json()["success"]
    assert login(other, "swimmer1", "longenough1").status_code == 200


def test_register_validation(client):
    assert client.post("/api/register", json={"username": "a", "password": "longenough1"}).status_code == 400
    assert client.post("/api/register", json={"username": "okname", "password": "short"}).status_code == 400
    client.post("/api/register", json={"username": "okname", "password": "longenough1"})
    dup = client.post("/api/register", json={"username": "OKNAME", "password": "longenough1"})
    assert dup.status_code == 400          # usernames are case-insensitive


def test_legacy_sha256_hashes_still_work_and_get_upgraded(client):
    """Hashes written by the old Node server (single or double SHA-256) must keep working."""
    pw = "OldPassword1"
    single = hashlib.sha256(pw.encode()).hexdigest()
    double = hashlib.sha256(single.encode()).hexdigest()
    for name, stored in (("nodeuser1", single), ("nodeuser2", double)):
        with db.connect() as c:
            c.execute("INSERT INTO users (id, username, password_hash, role, approved, created_at) "
                      "VALUES (?, ?, ?, 'User', 1, '2026-01-01 00:00:00')", (name, name, stored))
        assert login(client.application.test_client(), name, pw).status_code == 200
        assert not db.is_legacy_hash(db.get_user(name)["password_hash"])      # upgraded
        assert login(client.application.test_client(), name, pw).status_code == 200
        assert login(client.application.test_client(), name, "wrong-password").status_code == 401


def test_lockout_after_repeated_failures_and_admin_alert(client, admin):
    victim = client.application.test_client()
    for _ in range(cfg.MAX_FAILED_ATTEMPTS - 1):
        assert login(victim, "admin", "nope").status_code == 401
    assert login(victim, "admin", "nope").status_code == 423
    assert login(victim, "admin", "AdminPass123!").status_code == 423     # even the right password

    alerts = admin.get("/api/security_alerts").get_json()
    assert alerts and alerts[0]["kind"] == "ACCOUNT_LOCKED"

    assert admin.post("/api/users/unlock", json={"username": "admin"}).get_json()["success"]
    assert login(victim, "admin", "AdminPass123!").status_code == 200


def test_access_control(client, admin):
    anon = client.application.test_client()
    assert anon.get("/api/stats").status_code == 401
    assert anon.get("/api/users").status_code == 401
    assert anon.get("/cam1_feed").status_code == 401
    assert anon.get("/dashboard").status_code == 302

    client.post("/api/register", json={"username": "staff1", "password": "longenough1"})
    uid = next(u["id"] for u in admin.get("/api/users").get_json() if u["username"] == "staff1")
    admin.post("/api/users/approve", json={"userId": uid})
    staff = client.application.test_client()
    login(staff, "staff1", "longenough1")
    assert staff.get("/api/stats").status_code == 200
    assert staff.get("/api/users").status_code == 403
    assert staff.get("/api/incidents").status_code == 403
    assert staff.post("/api/save_roi", json={}).status_code == 403


def test_removed_account_loses_access_immediately(client, admin):
    client.post("/api/register", json={"username": "temp1", "password": "longenough1"})
    uid = next(u["id"] for u in admin.get("/api/users").get_json() if u["username"] == "temp1")
    admin.post("/api/users/approve", json={"userId": uid})
    temp = client.application.test_client()
    login(temp, "temp1", "longenough1")
    assert temp.get("/api/stats").status_code == 200
    admin.post("/api/users/reject", json={"userId": uid})
    assert temp.get("/api/stats").status_code == 401


def test_cannot_remove_self_or_last_admin(admin):
    me = next(u for u in admin.get("/api/users").get_json() if u["username"] == "admin")
    r = admin.post("/api/users/reject", json={"userId": me["id"]})
    assert r.status_code == 400
    ok, msg = db.delete_user(me["id"])
    assert not ok and "last administrator" in msg


def test_logout_is_post_only(admin):
    assert admin.get("/logout").status_code == 405
    assert admin.post("/logout").status_code == 302
    assert admin.get("/api/stats").status_code == 401


def test_dashboard_renders_per_role_and_sends_security_headers(client, admin):
    html = admin.get("/dashboard").get_data(as_text=True)
    assert 'id="incident-grid"' in html and "Cam" in html
    assert "Content-Security-Policy" in admin.get("/dashboard").headers

    client.post("/api/register", json={"username": "staff2", "password": "longenough1"})
    uid = next(u["id"] for u in admin.get("/api/users").get_json() if u["username"] == "staff2")
    admin.post("/api/users/approve", json={"userId": uid})
    staff = client.application.test_client()
    login(staff, "staff2", "longenough1")
    assert 'id="incident-grid"' not in staff.get("/dashboard").get_data(as_text=True)


def test_templates_are_csp_clean():
    """The strict CSP blocks inline scripts/styles, so templates must not use them."""
    import pathlib, re
    for path in pathlib.Path("lifeguard/templates").glob("*.html"):
        text = path.read_text()
        assert not re.search(r'\sstyle="', text), path
        assert not re.search(r'\son[a-z]+="', text), path
        assert "<script>" not in text, path


def test_admin_endpoints_validate_input(admin):
    assert admin.get("/api/login_logs?limit=abc").status_code == 200
    assert admin.get("/api/login_logs?limit=99999").status_code == 200
    assert admin.post("/api/save_roi", json={"cam_id": "cam9", "polygon": [[1, 1]] * 3}).status_code == 400
    assert admin.post("/api/save_roi", json={"cam_id": "cam1", "polygon": [[1, 1], [2, 2]]}).status_code == 400
    assert admin.post("/api/save_roi", json={"cam_id": "cam1", "polygon": [["a", 1]] * 3}).status_code == 400
    ok = admin.post("/api/save_roi", json={"cam_id": "cam1", "polygon": [[1, 1], [50, 1], [50, 50.7]]})
    assert ok.get_json()["success"]
    assert cfg.load_config()["cam1_roi"] == [[1, 1], [50, 1], [50, 50]]


def test_incident_endpoints(admin):
    iid = db.create_incident("cam1", 7, 61, b"\xff\xd8fakejpeg\xff\xd9")
    row = admin.get("/api/incidents").get_json()[0]
    assert row["id"] == iid and row["has_snapshot"] == 1 and row["has_video"] == 0
    assert admin.get(f"/api/incidents/{iid}/snapshot").mimetype == "image/jpeg"
    assert admin.get(f"/api/incidents/{iid}/video").status_code == 404
    db.attach_video([iid], b"mp4data", "video/mp4")
    assert admin.get(f"/api/incidents/{iid}/video").data == b"mp4data"
    assert admin.delete(f"/api/incidents/{iid}").get_json()["success"]
    assert admin.get("/api/incidents").get_json() == []


def test_stats_with_no_workers(admin):
    stats = admin.get("/api/stats").get_json()
    assert stats["total_in_pool"] == 0 and stats["total_submerged"] == 0
