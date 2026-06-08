"""Tests for the crowd-only swipe vote: dedup, anonymization, tallies, bot-check, rate-limit."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image
from starlette.testclient import TestClient

from e14detector.community import (
    CommunityStore,
    PollConfig,
    crop_id,
    field_key_of,
    issue_form_token,
    verify_form_token,
    voter_token,
)
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.webapp import create_app


# --- CommunityStore unit tests --------------------------------------------

def test_dedup_one_vote_per_identity(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.record_flag("k", "voter-a") is True
    assert store.record_flag("k", "voter-a") is False  # same identity, ignored
    assert store.record_flag("k", "voter-b") is True
    assert store.distinct_votes("k") == 2
    store.close()


def test_good_and_strange_tallies_are_independent(tmp_path: Path) -> None:
    """The two directions never share a counter; one identity may cast both."""
    store = CommunityStore(tmp_path / "c.sqlite")
    store.record_flag("k", "a")    # strange
    store.record_appeal("k", "a")  # good — same identity, opposite direction: both count
    assert store.strange_count("k") == 1
    assert store.good_count("k") == 1
    store.close()


def test_reviewed_actas_unions_flags_and_appeals(tmp_path: Path) -> None:
    """reviewed_actas() = mesas anyone voted on (flag OR 'se ve bien'), deduped by doc id."""
    store = CommunityStore(tmp_path / "c.sqlite")
    store.record_flag(field_key_of("doc1", 1, 1, None), "a")    # flagged
    store.record_appeal(field_key_of("doc2", 1, 1, None), "b")  # only "se ve bien"
    store.record_flag(field_key_of("doc3", 1, 1, None), "a")    # same mesa, both directions
    store.record_appeal(field_key_of("doc3", 1, 2, None), "b")
    assert store.reviewed_actas() == {"doc1", "doc2", "doc3"}
    store.close()


def test_counts_among_batches_both_directions(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    store.record_flag("k1", "a"); store.record_flag("k1", "b")  # 2 strange
    store.record_appeal("k1", "a")                              # 1 good
    store.record_appeal("k2", "z")                              # k2: 1 good only
    counts = store.counts_among(["k1", "k2", "k3"])
    assert counts["k1"] == {"good": 1, "strange": 2}
    assert counts["k2"] == {"good": 1, "strange": 0}
    assert counts["k3"] == {"good": 0, "strange": 0}  # never-voted key still present
    store.close()


def test_cid_register_and_resolve(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.resolve_cid("nope") is None
    store.register_cid("abc123", "doc:1:2:", "crops/x.png", "doc")
    row = store.resolve_cid("abc123")
    assert row["field_key"] == "doc:1:2:" and row["crop_rel"] == "crops/x.png"
    assert row["document_id"] == "doc"
    # Idempotent: re-registering the same cid does not raise or duplicate.
    store.register_cid("abc123", "doc:1:2:", "crops/x.png", "doc")
    store.close()


def test_hot_crops_ranks_by_strange_then_total(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    # k-hot: 3 strange; k-mid: 1 strange + 5 good; k-good: 4 good only.
    for v in ("a", "b", "c"):
        store.record_flag("k-hot", v)
    store.record_flag("k-mid", "a")
    for v in ("a", "b", "c", "d", "e"):
        store.record_appeal("k-mid", v)
    for v in ("a", "b", "c", "d"):
        store.record_appeal("k-good", v)
    hot = store.hot_crops(10)
    keys = [h["field_key"] for h in hot]
    # Strange weight dominates: k-hot (3 strange) > k-mid (1 strange) > k-good (0 strange).
    assert keys.index("k-hot") < keys.index("k-mid") < keys.index("k-good")
    assert hot[0] == {"field_key": "k-hot", "good": 0, "strange": 3}
    store.close()


def test_rate_limit_token_bucket(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    # Bucket of 2, negligible refill: third immediate call is denied.
    assert store.allow("t", refill_per_min=0.0, bucket=2) is True
    assert store.allow("t", refill_per_min=0.0, bucket=2) is True
    assert store.allow("t", refill_per_min=0.0, bucket=2) is False
    store.close()


def test_crop_id_is_opaque_and_deterministic() -> None:
    a = crop_id("secret", "doc1:1:1:")
    assert a == crop_id("secret", "doc1:1:1:")          # deterministic
    assert a != crop_id("secret", "doc1:1:2:")          # per field key
    assert a != crop_id("other", "doc1:1:1:")           # keyed by the secret
    assert re.fullmatch(r"[0-9a-f]{12}", a)             # opaque hex, leaks nothing
    assert "doc1" not in a


def test_form_token_roundtrip_and_timing() -> None:
    tok = issue_form_token("s", "sid1", now=1000.0)
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=3600, now=1000.5) is False
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=3600, now=1005.0) is True
    assert verify_form_token("s", "sid1", tok, min_age=2, max_age=10, now=1100.0) is False


def test_form_token_rejects_forgery_and_wrong_session() -> None:
    tok = issue_form_token("realsecret", "sid1", now=1000.0)
    assert verify_form_token("realsecret", "sid1", "1000.deadbeefdeadbeef", 0, 3600, now=1005.0) is False
    assert verify_form_token("wrongsecret", "sid1", tok, 0, 3600, now=1005.0) is False
    assert verify_form_token("realsecret", "sid2", tok, 0, 3600, now=1005.0) is False


def test_voter_token_is_per_ip_per_day_not_per_cookie() -> None:
    a = voter_token("salt", "1.1.1.1", day="2026-06-01")
    assert voter_token("salt", "2.2.2.2", day="2026-06-01") != a
    assert voter_token("salt", "1.1.1.1", day="2026-06-02") != a
    assert voter_token("salt", "1.1.1.1", day="2026-06-01") == a


# --- Webapp build helpers -------------------------------------------------

def _make_db(tmp_path: Path, docs_rows: dict[str, int], **doc_meta) -> tuple[Path, Path]:
    """Build a results DB with ``docs_rows`` = {document_id: n_candidate_rows}. One shared crop."""
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crop = output_dir / "crops" / "c.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), (255, 255, 255)).save(crop)
    from e14detector.storage import DetectorStore
    store = DetectorStore(db)
    for doc_id, n in docs_rows.items():
        store.upsert_document(DocumentMetadata(document_id=doc_id, source_path=f"{doc_id}.pdf", **doc_meta))
        for rn in range(1, n + 1):
            store.insert_vote_field(VoteField(
                document_id=doc_id, page_number=1, row_type="candidate", row_number=rn,
                candidate_name=f"Cand {rn}", raw_crop_path=str(crop),
            ))
    store.commit()
    store.close()
    return output_dir, db


def _build_app(tmp_path: Path, n_rows: int = 3, form_secret: str = "",
               rate_bucket: float = 10_000, turnstile_secret: str = "", turnstile_enabled: bool = False):
    output_dir, db = _make_db(tmp_path, {"doc1": n_rows})
    poll = PollConfig(rate_refill_per_min=0.0 if rate_bucket < 100 else 10_000, rate_bucket=rate_bucket,
                      turnstile_secret=turnstile_secret, turnstile_enabled=turnstile_enabled,
                      voter_salt="t", form_token_secret=form_secret, form_min_seconds=0.0)
    return create_app(results_db=db, output_dir=output_dir,
                      community_db=tmp_path / "community.sqlite", poll=poll)


def _feed_cid(client: TestClient, n: int = 5) -> str:
    return client.get(f"/api/feed?n={n}").json()["items"][0]["cid"]


# --- Feed / anonymization -------------------------------------------------

def test_feed_is_anonymized_and_cids_resolve(tmp_path: Path) -> None:
    app = _build_app(tmp_path, n_rows=3)
    client = TestClient(app)
    res = client.get("/api/feed?n=3")
    assert res.status_code == 200
    body = res.text
    # No acta id, path, or db column ever appears in the feed payload.
    assert "doc1" not in body and "crops/" not in body and "raw_crop_path" not in body
    items = res.json()["items"]
    assert items and all(set(it.keys()) == {"cid", "img_url"} for it in items)
    # A surfaced cid resolves server-side and its image is served by /c/{cid}.
    cid = items[0]["cid"]
    assert app.state.community.resolve_cid(cid)["document_id"] == "doc1"
    img = client.get(f"/c/{cid}")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")


def test_c_cid_unknown_is_404(tmp_path: Path) -> None:
    client = TestClient(_build_app(tmp_path))
    assert client.get("/c/deadbeef0000").status_code == 404


def test_acta_crops_returns_anonymized_siblings(tmp_path: Path) -> None:
    app = _build_app(tmp_path, n_rows=4)
    client = TestClient(app)
    cid = _feed_cid(client)
    res = client.get(f"/api/acta-crops?cid={cid}")
    assert res.status_code == 200
    body = res.text
    assert "doc1" not in body and "crops/" not in body
    items = res.json()["items"]
    assert len(items) == 4  # all candidate siblings of the same acta
    assert all(set(it.keys()) == {"cid", "img_url", "good", "strange"} for it in items)
    assert client.get("/api/acta-crops?cid=nope").status_code == 404


# --- Voting ---------------------------------------------------------------

def test_vote_good_and_strange_update_public_tallies(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    cid = _feed_cid(client)
    r1 = client.post("/api/vote", json={"cid": cid, "value": "strange"},
                     headers={"x-forwarded-for": "1.1.1.1"})
    assert r1.status_code == 200 and r1.json() == {"ok": True, "good": 0, "strange": 1}
    r2 = client.post("/api/vote", json={"cid": cid, "value": "good"},
                     headers={"x-forwarded-for": "2.2.2.2"})
    assert r2.json() == {"ok": True, "good": 1, "strange": 1}


def test_vote_duplicate_is_a_silent_noop(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    cid = _feed_cid(client)
    h = {"x-forwarded-for": "5.5.5.5"}
    assert client.post("/api/vote", json={"cid": cid, "value": "strange"}, headers=h).json()["strange"] == 1
    # Same identity + direction again: still 200, still 1 — never an error.
    dup = client.post("/api/vote", json={"cid": cid, "value": "strange"}, headers=h)
    assert dup.status_code == 200 and dup.json()["strange"] == 1


def test_vote_rejects_unknown_cid_and_bad_value(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    cid = _feed_cid(client)
    assert client.post("/api/vote", json={"cid": "ffff00001111", "value": "good"}).status_code == 404
    assert client.post("/api/vote", json={"cid": cid, "value": "maybe"}).status_code == 400
    assert client.post("/api/vote", json={"value": "good"}).status_code == 400


def test_vote_rate_limited_returns_429(tmp_path: Path) -> None:
    app = _build_app(tmp_path, rate_bucket=2)  # tiny bucket, no refill
    client = TestClient(app)
    cids = [it["cid"] for it in client.get("/api/feed?n=5").json()["items"]]
    h = {"x-forwarded-for": "7.7.7.7"}
    assert client.post("/api/vote", json={"cid": cids[0], "value": "good"}, headers=h).status_code == 200
    assert client.post("/api/vote", json={"cid": cids[1], "value": "good"}, headers=h).status_code == 200
    r = client.post("/api/vote", json={"cid": cids[2], "value": "good"}, headers=h)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_vote_requires_form_token_and_honeypot_drops_bots(tmp_path: Path) -> None:
    app = _build_app(tmp_path, form_secret="botsecret")
    client = TestClient(app)
    community = app.state.community
    cid = _feed_cid(client)

    # No token -> rejected, nothing recorded.
    r = client.post("/api/vote", json={"cid": cid, "value": "strange"}, headers={"x-forwarded-for": "1.1.1.1"})
    assert r.status_code == 403 and r.json()["error"] == "invalid_request"
    assert community.strange_count(community.resolve_cid(cid)["field_key"]) == 0

    # The /votar page issues a valid token; voting with it works (token is bound to the client IP,
    # so the page load and the vote must come from the same IP — as a real browser does).
    page = client.get("/votar", headers={"x-forwarded-for": "1.1.1.1"}).text
    tok = re.search(r'__formToken = "([^"]+)"', page).group(1)
    assert tok
    ok = client.post("/api/vote", json={"cid": cid, "value": "strange", "form_token": tok},
                     headers={"x-forwarded-for": "1.1.1.1"})
    assert ok.json()["ok"] is True and ok.json()["strange"] == 1

    # Honeypot filled -> looks like success to the bot, but no vote is recorded.
    fkey = community.resolve_cid(cid)["field_key"]
    hp = client.post("/api/vote", json={"cid": cid, "value": "strange", "form_token": tok, "website": "x"},
                     headers={"x-forwarded-for": "2.2.2.2"})
    assert hp.json() == {"ok": True}
    assert community.strange_count(fkey) == 1  # unchanged


def test_vote_turnstile_enabled_requires_session_form_token(tmp_path: Path) -> None:
    """Turnstile is a SESSION gate: with it on (and the prod form-token secret set), a vote with no
    form token is rejected by bot_check. The legit path mints that token via /api/session after a
    solve; votes never carry a per-vote Turnstile token (see test_webapp's success case)."""
    app = _build_app(tmp_path, form_secret="s", turnstile_secret="x", turnstile_enabled=True)
    client = TestClient(app)
    cid = _feed_cid(client)
    r = client.post("/api/vote", json={"cid": cid, "value": "good"}, headers={"x-forwarded-for": "9.9.9.9"})
    assert r.status_code == 403
    assert app.state.community.good_count(app.state.community.resolve_cid(cid)["field_key"]) == 0


def test_vote_works_without_local_crop_file(tmp_path: Path) -> None:
    """National deploy: crops live on the CDN, not the volume. Voting resolves the cid and
    records without ever touching the file, so a missing local crop must not block a vote."""
    app = _build_app(tmp_path)
    client = TestClient(app)
    cid = _feed_cid(client)
    (tmp_path / "out" / "crops" / "c.png").unlink()  # gone, as on the Fly volume
    r = client.post("/api/vote", json={"cid": cid, "value": "strange"}, headers={"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 200 and r.json()["strange"] == 1


# --- Pages ----------------------------------------------------------------

def test_billboard_shows_public_counts(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    cid = _feed_cid(client)
    client.post("/api/vote", json={"cid": cid, "value": "strange"}, headers={"x-forwarded-for": "1.1.1.1"})
    items = client.get("/api/billboard").json()["items"]
    assert items and items[0]["strange"] == 1
    # The billboard is intentionally de-anonymized (public tally + acta link); each card
    # carries the document id and a location label so it can link through to /acta.
    assert set(items[0].keys()) == {"cid", "img_url", "document_id", "loc", "good", "strange"}
    assert items[0]["document_id"] == "doc1"


def test_votar_page_renders(tmp_path: Path) -> None:
    client = TestClient(_build_app(tmp_path))
    page = client.get("/votar")
    assert page.status_code == 200
    # The grid-voting page loads a whole acta and submits it as a batch.
    assert "/api/acta-deck" in page.text and "/api/vote-batch" in page.text


def test_acta_page_is_readonly_with_public_counts(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    # Cast a strange vote on doc1's first row, then load the acta page.
    fkey = field_key_of("doc1", 1, 1, None)
    app.state.community.record_flag(fkey, "voterX")
    page = client.get("/acta/doc1").text
    assert page.count("la ven extra") >= 1  # public tally rendered
    # No voting affordances remain on the acta page.
    assert "/api/flag" not in page and "/api/vote" not in page
    assert 'class="flag"' not in page and 'class="normal"' not in page
    assert "Marcar como" not in page
    # Links to the feed instead.
    assert "/votar" in page


def test_buscar_floats_most_voted_to_top_silently(tmp_path: Path) -> None:
    # The directory list lives at /buscar since the /browse split (bare /browse now 307s to /votar).
    output_dir, db = _make_db(tmp_path, {"doc-a": 1, "doc-b": 1, "doc-c": 1})
    community = CommunityStore(tmp_path / "community.sqlite")
    community.record_flag(field_key_of("doc-c", 1, 1, None), "v1")
    community.record_flag(field_key_of("doc-c", 1, 1, None), "v2")
    community.record_flag(field_key_of("doc-b", 1, 1, None), "v1")
    community.close()
    app = create_app(results_db=db, output_dir=output_dir, community_db=tmp_path / "community.sqlite")
    html = TestClient(app).get("/buscar").text
    pos = {d: html.index(f"/acta/{d}") for d in ("doc-a", "doc-b", "doc-c")}
    assert pos["doc-c"] < pos["doc-b"] < pos["doc-a"]   # vote order beats id order
    assert "2 votos" not in html and "votantes" not in html  # counts stay silent here


def test_browse_review_lists_only_voted_actas(tmp_path: Path) -> None:
    output_dir, db = _make_db(tmp_path, {"doc-voted": 1, "doc-plain": 1},
                              department_code="01", department_name="ANTIOQUIA")
    community = CommunityStore(tmp_path / "community.sqlite")
    community.record_flag(field_key_of("doc-voted", 1, 1, None), "v1")
    community.close()
    client = TestClient(create_app(results_db=db, output_dir=output_dir,
                                   community_db=tmp_path / "community.sqlite"))
    review = client.get("/browse?review=1").text
    assert "/acta/doc-voted" in review
    assert "/acta/doc-plain" not in review        # no community votes -> not listed
    # The feed CTA shows on the normal browse view too.
    assert "/votar" in client.get("/browse").text


def test_high_vote_label_shows_from_crowd_alone(tmp_path: Path, monkeypatch) -> None:
    from e14detector import config
    monkeypatch.setattr(config, "HIGH_VOTE_THRESHOLD", 3)
    output_dir, db = _make_db(tmp_path, {"doc1": 1})
    community = CommunityStore(tmp_path / "community.sqlite")
    fkey = field_key_of("doc1", 1, 1, None)
    for i in range(3):  # 3 distinct strange voters >= threshold
        community.record_flag(fkey, f"voter{i}")
    community.close()
    client = TestClient(create_app(results_db=db, output_dir=output_dir,
                                   community_db=tmp_path / "community.sqlite"))
    acta = client.get("/acta/doc1").text
    assert "muy reportada por la comunidad" in acta
    # The directory chip lives at /buscar since the /browse split (bare /browse now 307s to /votar).
    assert client.get("/buscar").text.count("muy reportada por la comunidad") >= 1


# --- Operator admin path --------------------------------------------------

def test_admin_poll_is_token_gated(tmp_path: Path, monkeypatch) -> None:
    from e14detector import config as _config
    output_dir, db = _make_db(tmp_path, {"doc1": 1},
                              department_name="ANTIOQUIA", municipality_name="MEDELLIN", mesa="003")
    community = CommunityStore(tmp_path / "community.sqlite")
    fk = field_key_of("doc1", 1, 1, None)
    community.record_flag(fk, "v1"); community.record_flag(fk, "v2")
    community.close()
    client = TestClient(create_app(results_db=db, output_dir=output_dir,
                                   community_db=tmp_path / "community.sqlite"))
    monkeypatch.setattr(_config, "ADMIN_TOKEN", "")
    assert client.get("/admin/poll").status_code == 404            # disabled
    monkeypatch.setattr(_config, "ADMIN_TOKEN", "s3cret")
    assert client.get("/admin/poll").status_code == 403            # no key
    assert client.get("/admin/poll?key=wrong").status_code == 403
    ok = client.get("/admin/poll?key=s3cret")
    # The admin board now renders the count-model reconciliation + DB-management panel (the old
    # per-candidate votes table was retired); the token gate is what this test guards.
    assert ok.status_code == 200 and "Reconciliación de conteos" in ok.text


# --- Optional named contributor identity ----------------------------------

def test_claim_assigns_pin_and_counts_reviews(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    res = store.claim_contributor("Aguila Del Valle", "sid-1")
    assert res["ok"] and res["reviews"] == 0 and res["devices"] == 1
    assert re.fullmatch(r"\d{4}", res["pin"])             # server-assigned 4-digit pin
    me = store.contributor_me("sid-1")
    assert me["display_name"] == "Aguila Del Valle" and me["rank"] == 1
    # Crediting two distinct mesas bumps the count; re-crediting one is idempotent.
    assert store.credit_review("sid-1", "mesaA") == 1
    assert store.credit_review("sid-1", "mesaB") == 2
    assert store.credit_review("sid-1", "mesaA") == 2     # dedup: same mesa never double-counts
    assert store.contributor_me("sid-1")["reviews"] == 2
    store.close()


def test_nickname_is_unique_normalized(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.claim_contributor("Centinela", "sid-1")["ok"]
    # Same name in different casing/spacing is the SAME identity -> taken.
    assert store.claim_contributor("  centinela ", "sid-2") == {"ok": False, "error": "taken"}
    # A device that already has an identity can't silently grab a second.
    assert store.claim_contributor("Otra", "sid-1") == {"ok": False, "error": "already_linked"}
    store.close()


def test_claim_rejects_invalid_and_blocked_names(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.claim_contributor("a", "s")["error"] == "too_short"
    assert store.claim_contributor("x" * 21, "s")["error"] == "too_long"
    assert store.claim_contributor("<script>", "s")["error"] == "invalid"
    assert store.claim_contributor("puta", "s")["error"] == "blocked"
    store.close()


def test_link_requires_pin_and_caps_devices(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    pin = store.claim_contributor("Guardian", "sid-1")["pin"]
    store.credit_review("sid-1", "mesaA")                 # count lives on the nickname
    assert store.link_contributor("Guardian", "0000" if pin != "0000" else "1111", "sid-2")["error"] == "bad_pin"
    assert store.link_contributor("Nadie", pin, "sid-2")["error"] == "not_found"
    # Correct pin links a 2nd device; the review count follows the identity, not the device.
    linked = store.link_contributor("Guardian", pin, "sid-2")
    assert linked["ok"] and linked["devices"] == 2 and linked["reviews"] == 1
    assert store.contributor_me("sid-2")["display_name"] == "Guardian"
    # 3rd device ok, 4th refused (DEVICE_LIMIT=3). Re-linking an existing device is idempotent.
    assert store.link_contributor("Guardian", pin, "sid-3")["ok"]
    assert store.link_contributor("Guardian", pin, "sid-2")["ok"]            # idempotent, still 3
    assert store.link_contributor("Guardian", pin, "sid-4")["error"] == "device_limit"
    store.close()


def test_credit_review_is_noop_for_anonymous_device(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    assert store.credit_review("anon-sid", "mesaA") is None
    assert store.contributor_me("anon-sid") is None
    store.close()


def test_leaderboard_ranks_by_reviews(tmp_path: Path) -> None:
    store = CommunityStore(tmp_path / "c.sqlite")
    store.claim_contributor("Ana", "a"); store.claim_contributor("Boris", "b")
    for m in ("m1", "m2", "m3"):
        store.credit_review("b", m)
    store.credit_review("a", "m1")
    board = store.leaderboard(10)
    assert [r["display_name"] for r in board] == ["Boris", "Ana"]
    assert board[0]["reviews"] == 3
    assert store.contributor_me("b")["rank"] == 1 and store.contributor_me("a")["rank"] == 2
    store.close()


# --- Contributor endpoints (sid cookie carries the identity) --------------

def test_contributor_claim_link_and_me_endpoints(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    client = TestClient(app)
    # Anonymous to start.
    assert client.get("/api/contributor/me").json() == {"ok": True, "contributor": None}
    # Claim a name -> get a pin once, and the sid cookie now carries the identity.
    res = client.post("/api/contributor/claim", json={"nickname": "Colibri"})
    body = res.json()
    assert res.status_code == 200 and body["ok"] and re.fullmatch(r"\d{4}", body["pin"])
    me = client.get("/api/contributor/me").json()["contributor"]
    assert me["display_name"] == "Colibri" and me["pin"] == body["pin"]
    # A different device (fresh cookie jar) links via the pin.
    other = TestClient(app)
    bad = other.post("/api/contributor/link", json={"nickname": "Colibri", "pin": "9999" if body["pin"] != "9999" else "1111"})
    assert bad.json()["error"] == "bad_pin"
    ok = other.post("/api/contributor/link", json={"nickname": "Colibri", "pin": body["pin"]})
    assert ok.status_code == 200 and ok.json()["ok"] and ok.json()["devices"] == 2


def test_contributor_credit_flows_through_vote_batch(tmp_path: Path) -> None:
    app = _build_app(tmp_path, n_rows=3)
    client = TestClient(app)
    assert client.post("/api/contributor/claim", json={"nickname": "Veedora"}).json()["ok"]
    # Submitting a mesa (one vote-batch) credits the named contributor exactly once.
    cids = [it["cid"] for it in client.get("/api/feed?n=3").json()["items"]]
    r = client.post("/api/vote-batch", json={"strange": cids[:1], "good": cids[1:]})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/contributor/me").json()["contributor"]["reviews"] == 1


def test_contributor_leaderboard_endpoint_includes_self(tmp_path: Path) -> None:
    app = _build_app(tmp_path, n_rows=3)
    client = TestClient(app)
    client.post("/api/contributor/claim", json={"nickname": "Lider"})
    cids = [it["cid"] for it in client.get("/api/feed?n=3").json()["items"]]
    client.post("/api/vote-batch", json={"strange": [], "good": cids})
    board = client.get("/api/contributor/leaderboard").json()
    assert board["items"][0] == {"display_name": "Lider", "reviews": 1}
    assert board["me"]["rank"] == 1 and board["me"]["reviews"] == 1
