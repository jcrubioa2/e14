"""Smoke-test PgCommunityStore against the live Aurora DB. Self-cleaning."""
import sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from e14detector.community_pg import PgCommunityStore
from e14detector.vote_aws import vote_client

CL = os.environ["AURORA_CLUSTER_ARN"]
SE = os.environ["AURORA_SECRET_ARN"]
s = PgCommunityStore(CL, SE, database="e14", region="us-east-1")

P = f"smoketest:{int(time.time())}"          # unique field-key prefix
fk1, fk2 = f"{P}:1:1:A", f"{P}:1:2:B"
ok = []

def check(name, cond):
    ok.append(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

# flags + dedup
check("record_flag new -> True", s.record_flag(fk1, "voterA") is True)
check("record_flag dup -> False", s.record_flag(fk1, "voterA") is False)
check("record_flag 2nd voter -> True", s.record_flag(fk1, "voterB") is True)
check("distinct_votes == 2", s.distinct_votes(fk1) == 2)

# appeals (separate direction)
check("record_appeal new -> True", s.record_appeal(fk1, "voterC") is True)
check("distinct_appeals == 1", s.distinct_appeals(fk1) == 1)
check("flags unaffected by appeal", s.distinct_votes(fk1) == 2)

# counts_among (batch, ANY array param)
s.record_flag(fk2, "voterA")
c = s.counts_among([fk1, fk2])
check("counts_among fk1 strange=2 good=1", c[fk1] == {"strange": 2, "good": 1})
check("counts_among fk2 strange=1 good=0", c[fk2] == {"strange": 1, "good": 0})

# cid round-trip (unique per run so a crashed prior run can't shadow it)
cid = f"cid{P[-9:]}"
s.register_cid(cid, fk1, "crops/x.png", P)
r = s.resolve_cid(cid)
check("resolve_cid field_key", r and r["field_key"] == fk1)
check("resolve_cid crop_rel", r and r["crop_rel"] == "crops/x.png")
check("resolve_cid missing -> None", s.resolve_cid("nope-"+P[-6:]) is None)

# batch register
b1, b2 = "c1"+P[-7:], "c2"+P[-7:]
s.register_cids([(b1, fk1, "a.png", P), (b2, fk2, "b.png", P)])
check("register_cids batch resolves", s.resolve_cid(b1) is not None)

# rate limit
tok = f"rl:{P}"
check("allow first -> True", s.allow(tok, refill_per_min=10, bucket=2) is True)
check("allow second -> True", s.allow(tok, refill_per_min=10, bucket=2) is True)
check("allow third (empty) -> False", s.allow(tok, refill_per_min=10, bucket=2) is False)

# high_voted_fields + hot_crops + verdict/admin
check("high_voted_fields >=2 includes fk1", fk1 in s.high_voted_fields(2))
hot = s.hot_crops(50)
check("hot_crops includes fk1", any(h["field_key"] == fk1 for h in hot))
s.record_verdict(fk1, strange=True, votes_at_call=2)
st = s.state_of(fk1)
check("state_of vlm_state STRANGE", st and st["vlm_state"] == "STRANGE")
check("published_among includes fk1", fk1 in s.published_among([fk1, fk2]))
ov = {r["field_key"]: r for r in s.admin_overview()}
check("admin_overview fk1 votes=2", ov.get(fk1, {}).get("votes") == 2)
check("acta_popularity has prefix", s.acta_popularity().get(P, 0) >= 1)

# cleanup
client = vote_client("rds-data")
base = dict(resourceArn=CL, secretArn=SE, database="e14")
for sql in (
    f"DELETE FROM flags WHERE field_key LIKE '{P}%'",
    f"DELETE FROM appeals WHERE field_key LIKE '{P}%'",
    f"DELETE FROM field_state WHERE field_key LIKE '{P}%'",
    f"DELETE FROM cid_index WHERE document_id = '{P}'",
    f"DELETE FROM rate_buckets WHERE voter_token = 'rl:{P}'",
):
    client.execute_statement(**base, sql=sql)
print("  (cleaned up test rows)")

print(f"\n{sum(ok)}/{len(ok)} checks passed")
sys.exit(0 if all(ok) else 1)
