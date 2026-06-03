"""Phase 4/5 smoke test: SQS publisher + vote_worker, against live SQS + Aurora.

1. Unit-level: drain_once deletes ONLY after a successful insert (zero-loss order),
   and leaves messages on the queue when the DB write fails.
2. End-to-end: publish via VotePublisher, drain with the real worker, assert dedup.
3. _parse poison handling.
Self-cleaning.
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from e14detector.community_pg import PgCommunityStore
from e14detector.vote_queue import VotePublisher
from e14detector import vote_worker
from e14detector.vote_aws import vote_client

CL = os.environ["AURORA_CLUSTER_ARN"]
SE = os.environ["AURORA_SECRET_ARN"]
QURL = os.environ["SQS_QUEUE_URL"]
ok = []

def check(name, cond):
    ok.append(bool(cond)); print(f"  {'PASS' if cond else 'FAIL'}  {name}")

# ---- 1. deterministic drain_once with fakes (zero-loss ordering) ----
class FakeSqs:
    def __init__(self, messages):
        self._messages = messages
        self.deleted = []
    def receive_message(self, **_):
        return {"Messages": self._messages}
    def delete_message_batch(self, QueueUrl, Entries):
        self.deleted.extend(Entries); return {}

class RaisingStore:
    def record_votes_batch(self, strange, good):
        raise RuntimeError("DB down")

class OkStore:
    def __init__(self): self.calls = []
    def record_votes_batch(self, strange, good):
        self.calls.append((list(strange), list(good)))

msgs = [
    {"MessageId": "1", "ReceiptHandle": "r1", "Body": '{"field_key":"k1","voter_token":"v1","direction":"strange"}'},
    {"MessageId": "2", "ReceiptHandle": "r2", "Body": '{"field_key":"k2","voter_token":"v2","direction":"good"}'},
    {"MessageId": "3", "ReceiptHandle": "r3", "Body": 'not json'},  # poison
]
# DB down: must raise and delete NOTHING
f = FakeSqs(msgs)
try:
    vote_worker.drain_once(f, "q", RaisingStore()); raised = False
except RuntimeError:
    raised = True
check("insert failure propagates", raised)
check("nothing deleted on DB failure (zero-loss)", f.deleted == [])

# DB ok: deletes the 2 valid, leaves the poison, batches by direction
f2 = FakeSqs(msgs); st = OkStore()
processed, poison = vote_worker.drain_once(f2, "q", st)
check("processed 2 valid", processed == 2)
check("counted 1 poison", poison == 1)
check("deleted exactly the 2 valid", len(f2.deleted) == 2)
check("strange batch = [(k1,v1)]", st.calls and st.calls[0][0] == [("k1", "v1")])
check("good batch = [(k2,v2)]", st.calls and st.calls[0][1] == [("k2", "v2")])

# ---- 3. _parse poison cases ----
check("_parse bad direction -> None",
      vote_worker._parse({"Body": '{"field_key":"k","voter_token":"v","direction":"x"}'}) is None)
check("_parse missing field -> None",
      vote_worker._parse({"Body": '{"field_key":"k"}'}) is None)
check("_parse valid", vote_worker._parse(
      {"Body": '{"field_key":"k","voter_token":"v","direction":"good"}'}) == ("k", "v", "good"))

# ---- 2. end-to-end: publish -> drain -> dedup ----
store = PgCommunityStore(CL, SE, database="e14", region="us-east-1")
P = f"wtest:{int(time.time())}"
fkS, fkG = f"{P}:1:1:S", f"{P}:1:2:G"
pub = VotePublisher(QURL)
pub.publish(fkS, "voterX", "strange")
pub.publish(fkS, "voterX", "strange")  # duplicate
pub.publish(fkS, "voterY", "strange")
pub.publish(fkG, "voterZ", "good")

sqs = vote_client("sqs")
seen = 0
deadline = time.time() + 90
while seen < 4 and time.time() < deadline:
    p, _ = vote_worker.drain_once(sqs, QURL, store)
    seen += p

check("e2e drained >=4 messages", seen >= 4)
check("e2e dedup: fkS distinct strange == 2", store.distinct_votes(fkS) == 2)
check("e2e fkG distinct good == 1", store.distinct_appeals(fkG) == 1)

# cleanup
c = vote_client("rds-data"); base = dict(resourceArn=CL, secretArn=SE, database="e14")
for sql in (f"DELETE FROM flags WHERE field_key LIKE '{P}%'",
            f"DELETE FROM appeals WHERE field_key LIKE '{P}%'"):
    c.execute_statement(**base, sql=sql)
print("  (cleaned up)")

print(f"\n{sum(ok)}/{len(ok)} checks passed")
sys.exit(0 if all(ok) else 1)
