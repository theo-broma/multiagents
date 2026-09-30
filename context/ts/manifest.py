"""manifest.py OUT.tsv chunk*.xml -> sorted 'nodeid<TAB>outcome' lines from pytest junitxml."""
import sys, os, xml.etree.ElementTree as ET
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def nodeid(classname, name):
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        path = "/".join(parts[:i]) + ".py"
        if os.path.exists(os.path.join(ROOT, path)):
            return "::".join([path] + parts[i:] + [name])
    raise SystemExit(f"cannot map {classname}::{name}")
# pytest writes a second <testcase> for the same id when a test fails in
# call and again in teardown: the outcomes are merged, never overwritten.
kinds_by_id = {}
for f in sys.argv[2:]:
    for tc in ET.parse(f).getroot().iter("testcase"):
        kinds = kinds_by_id.setdefault(nodeid(tc.get("classname"), tc.get("name")), [])
        for child in tc:
            if child.tag == "failure": kind = "failed"
            elif child.tag == "error": kind = "error"
            elif child.tag == "skipped":
                kind = "xfailed" if child.get("type") == "pytest.xfail" else "skipped"
            else: continue
            if kind not in kinds: kinds.append(kind)
rows = {nid: "+".join(kinds) or "passed" for nid, kinds in kinds_by_id.items()}
with open(sys.argv[1], "w") as fh:
    for nid in sorted(rows):
        fh.write(f"{nid}\t{rows[nid]}\n")
from collections import Counter
print(sys.argv[1], len(rows), dict(Counter(rows.values())))
