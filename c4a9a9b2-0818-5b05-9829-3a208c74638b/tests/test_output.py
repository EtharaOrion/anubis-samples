#!/usr/bin/env python3
"""Verifier for the admission hardening config repair task.

Re-derives the hardened admission configuration for the active enforcement class
from the interacting numeric policy layers under /app/data using integer
round-half-even arithmetic, then checks that the agent's artifacts under /app/out
match that derivation, that the repaired task-ctl CLI actually built
the config, and that no cheat shortcuts were taken.
"""
from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

OUT = Path("/app/out")


_EMBEDDED_DATA_B64 = (
    "H4sIAF79oGoC/719S3MkSXIeJJ0AiTQ+lpIOkqlseNAF0xbu8W7s0Gy1OxRXXM3StoeiaLQ1WDVQ6KkdoAqsKvRMi71m+gu666j/"
    "oZNu+hH6AzrrooMiH5Hp8cjsBMbFtUFvZ8Yj3f37PPyRVY0/ODs7+53wc7u5Wz/dn46vfnPc7370D8/O/u5itfrs+nZ/89nr1Wdv"
    "18fNan37sD0et/vd5zf73emwv1+dnnbb3bvV/frD5nC12r/fHMJfj6vtrrt13dw5bG833a6r9e529bjd7Ta3+chhc7fdbVanbzar"
    "x8PmZnO72d1sVnfbzf3t8bPLRpKbbzY33x6fHhppbg43EvvbnSTXj/fr3aYZG4V8e9h/uzl007YP63eb68PT7rR9aKftb7afh8ub"
    "bvhhf9vePZ4O25vT5/f7m/V9N9Iofh0ecrd9FyY0Nmmmr7+/DlLuD0G41yuU+rK7f9icDh+u77cP21O4b/q7zSP3T6frh36yj/cP"
    "6+19EDA895v14e3+8Prmfvt596zXX//kzZ+/Fv3/cKM1ems/Cwt/24rVmTdiNkp23LS6X59O99fHIOGulRCcEP1Dd/tg2Ovvtrvb"
    "/XedRGoYa9QKS26eDofN7nTd79XMMap58sVvfy8QJQB+CCPXh/Vp8+p0fP9//+js7OZ+He5sb8+Pp/Xp6Xge5/zt4/H8sHkXdjnf"
    "7U+bC3H+k5/98i++/vJn54Di/N39/u36/vxmHYTaBotfwDCMqjKMw7A0lWE5DGtRGVbDsKsNi/Of/eonf/p1++TN0/ltMN6Hz2/3"
    "T2/vN8MYoD0P2t0e1nenz/d3d8fNKcj85s/Cxn91rly5EIZNy4UYF1osF2K/UJpyoTx/85d/8eWv3nz5s8aOgR7Fatmv1qJcraIu"
    "prIwDrrKwsEIYd1BnN9t7wN5xSApCDg/QH8bwu1fffkffvnnrYR4fsB+ABu1qfTy/CD7MRke35sEhDo/qP6+Io/W5wfd39Zh+vgM"
    "c34w/YAhMtlRVJsbzo3yuvCIcS8/yusHCAHEKCqIET4AGGUFGAkBgKO0gHSFHKUFmckFapQZ1IgXaGJfHR4zCAxmFBhM2G4csURm"
    "S5UER4R2o4XBE5k9XRFYOgiNolGTCI0wCo1AHAZHoRHD/WgClIQUktIF1SgzKqon6lFm1LkEZhQcTfMkOmiJ7JaK4YjcLkMCPRHe"
    "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"
    "hatuWlUMqooB1KkC9RQCZHUTVZGtlkVAJRcCsNUnuXoMAfAVi2Mtv0Oo3cQSG5w4LgBVwRpAXdu1xkVAW56sgFXM0JfWkpVkT0LF"
    "1GNGkckvJxIlGJOLfEDXgxrIGnSyCp10NWPIMmECJSpxAqp5Biis7aqqSe3EMQ9KT7ixquJHc45xc1eVbiLpAF1jpi7PR9ATyS7o"
    "MjcEXR76oHVNsKnUA7QtuZ3kHYOwvgayqZDTlMkuGKwUE3LCWKaWIILRlUdVATN2woamliiCqZZYVkywx06dlRYrPLFywvBTSQhY"
    "XRPSmpIrdiJvBFslp60dmU5MgOCg5JvDyknmZAmtU7XY43RlS1Pb0tYQcWVxDK4a5GgyMtSflVwYfC1ujwnJPz47O3va3l4/rB9f"
    "bXbv/90/Pzv741Xb+1n95c9/tgq3r1ZvfvrLv/jy+se79cPmT774cZj9J1ervkETJhy+3RxW99vd5rh6tz5tVu/X99vb7enDxR+v"
    "bjc3+w/NPterx8Pmbvv95rafuT5sVqfD+jH8bXe7eng6nlZvN6vtu93+sLm96B74k5/9+59/9UWIeCLe+PrrL998/YU0w50v/+OX"
    "P/0Cx+tf/LwZd8P1r778yc++UGK4fvPzf/vVFzju+Fe/+vnXX34hmxuNlD/78qe//Otr8QUk1xCugVxjuEZyLcO1JNcqXCtyrcO1"
    "JtcmXBtybcO1JdcuXDty7cO1p/IEASERMEgIcPHVL7/+MgjfgRdQWh1P63fb3buVbgzQjkJ9tF+L9VHsRmV9VHajqj6qulFdH9Xd"
    "qKmPmm7U1kdtN+rqo64b9fVR31ujbiyIxqpbK1oa6uaC3lxQtxf09oK6waA3GNQtBr3FoG4y6E0GdZtBbzOoGw16o0HdatBbDetW"
    "w95qWLcaRo7VrYa91bBuNeythnWrYW81rFsNe6th3WrYWw3rVsPeali3GvZWw7rVsLearFtN9laTdavJ3mqybjUZXbNuNdlbTdat"
    "JnurybrVZG81Wbea7K0m61aTvdVk3Wqyt5qsW032VlN1q6neaqpuNdVbTdWtpnqrqbrVVDzR6lZTvdVU3Wqqt5qqW031VlN1q6ne"
    "aqpuNdVbTdWtpnqr6brVdG81Xbea7q2m61bTvdV03Wq6t5quW03HQFC3mu6tputW073VdN1qurearltN91bTdavp3mqmbjXTW83U"
    "rWZ6q5m61UxvNVO3mumtZupWM73VTN1qJsbPutVMbzVTt5rprWbqVjO91Uzdaqa3mq1bzfZWs3Wr2d5qtm4121vN1q1me6vZutVs"
    "bzVbt5rtrWbrVrMx7ahbzfZWs3Wr2d5qtm4121vN1a3mequ5utVcbzVXt5rrrebqVnO91Vzdaq63mqtbzfVWc3Wrud5qrm41F7O1"
    "utVcbzVXt5rrrebrVvO91Xzdar63mq9bzfdW83Wr+d5qvm4131vN163me6v5utV8bzVft5rvrebrVvMxya1bzQ9ZbtVsMNQEAurj"
    "Mc8VWB+Pia6Q9fGY6QpVH4+prtD18ZjrClMfj8musPXxmO0KVx+P6a7w9fFov3qZAGOZULffUCfUCwUYCoV6pQBDpVAvFWAoFeq1"
    "Agy1Qr1YgKFYqFcLMFQL9XIBhnKhXi9ArBegXjBALBigXjEADnVW3X6xZIB6zQCxZoB60QCxaIB61QCxaoB62QCxbIB63QCxboB6"
    "4QCxcIB65QCxcoB66QCxdIB67QCxdoB68QByKFTr9ovVA9TLB4jlA9TrB4j1A9QLCIgFBNQrCIgVBNRLCIglBNRrCIg1BNSLCIhF"
    "BNSrCIhVBNTLCIhlBNTrCFBDpV+3XywkoF5JQKwkoF5KQCwloF5LQKwloF5MQCwmoF5NQKwmoF5OQCwnoF5PQKwnoF5QQCwooF5R"
    "QKwooF5SgB5aJXX7xZoC6kUFxKIC6lUFxKoC6mUFxLIC6nUFxLoC6oUFxMIC6pUFxMoC6qUFxNIC6rUFxNoC6sUFxOIC6tUFmKHX"
    "VLdfLC+gXl9ArC+gXmBALDCgXmFArDCgXmJALDGgXmNArDGgXmRALDKgXmVArDKgXmZALDOgXmdArDOgXmiAHZp1dfvFSgPqpQbE"
    "UgPqtQbEWgPqxQbEYgPq1QbEagPq5QbEcgPq9Ua4ffFPz87Oml8u0/T8r2/2u9Nhf398dbw5vv/vPxo/cXq1P9xuDlf9eHPju832"
    "3Tenq5v14fDh+u5+/e6q+1DqhbgSVzefHzc3N/uHxytsLvsXEWEIwtBuvzvs96cr0MkQhqHm85T73f2HK8AwdRyTYexm/Xi8Apes"
    "UeH++vFxfXjYH65AkDWQSkEfBYkUiYCQSaGTHQcpEMkaTJ4kRTKUPEknQ8mTMF02PskRCTDTVyVrdPeozSk1nkzES+wnU/HSVTiY"
    "3JAlKlVWJ0N0t8QOKlOWmlWNykqZrEmURZGsIcq6ZJEJA3fH95vD9pThJ668D4Pt+64r3YjXvWprBlwYePfN/ni6Us1z2pd1AfCJ"
    "BTCxAKcW4MQCObVATixQUwvUpEgoGsTumrniqn832QAiGrTuGl+Aq/GNawO8aMC6awhKNEbRwHQnrxrSkLsNSHcNViLfpYHoTl/p"
    "QZbg4qKFx1w1lIomQGGbm/bKhplRwOZ2o9Cdu3Lp1s1DGxvc+StfiA6trs1BIIYnBEWg0zWoCtQy0CraeAsOs8P20Goa/mz+G2WH"
    "VtXwZ+t34+1Wz4ZperRMc79VNfzZ+g/RC1p1w5/Nf0SYVt3wZ0voOL8Rp9U2/Nn8NwyEFdgqi6I7PCJBEFtlsVmZze9wxe74IoYL"
    "O2Crc/gTZbao1Tr8iYpihq3W4U/UCR+w1Tr8iYY6GGKrdPgTbQpa84hW8/AnOmpYbPUOf2Kmt2z1lh2jyVZBRtnqLntWD0+XreYy"
    "p7TsKJ1wutm/VVn2pI4OibJVWXaMzh7bai07XkdJm+e2WsuC2rLVWBbcbh7eai0Lcoch1eqtenIPjo+qVVr17B71UK3Wqqf3qIhq"
    "1VY9v1PXUp0/5yRXre6qJzk5RVSruepZTp7daq4Skjfbt4qrjOTNPq3aqid5al3d6q0TnocL3aqtM54381u9dc/zwcF0q7buKZ6a"
    "Vrdq657lhGu6O8VSnoctdKu37nk+uIVu1dZVmutWd93TnMrb6q57oqeLTKu7KbgetDat/qY4wYNpTGsB0/Gd8M60JjAJ4ZvbrfYm"
    "J7xpdTf0CG/udkd4x/X09DWt9ialeyNOq7kp6N4Mtbqbnu7Do22rto1UT+hgW71tpHsigW0Vt8mJHqxgW71tz3hiettqbhO6B9Pa"
    "VnPb033cplXdZkd6M7+LYAnbG2lavW3P9hQ92ypuI+GHM8m1mruE7OHRrlXa9WQfDeVahV3PdWJz16rserYPmrlWYZcRvRloVXaR"
    "6IlVXau3i4d6goZrVXc94wnXXBe+k1O9md/q7SLZB0V8q7fviT6cI77V23ckH1zft1r7lN9hc98q7Tt+D7bzrc6+SFKajVqtfX+m"
    "j8K0CnuapzS7t6r6jt7j8dsmXne+YzcVpstSMmpL0acoJbebwS5RET25R3PKLikD0VN7RFp2iRmI2nkuu/wMRM/wQWrZpWcg0ryl"
    "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"
    "L53NJ7pL53IOSX/pEq3CsxVxtdQ0anQ4wlhF3G1kvBodbrSCIg5H7o4uNwimiMuRmaPTDSCo0uEaJUaPG5dr4nIpyXThdc1JUHhd"
    "M5N43fA0TTxunFl1uCCxrjmcHh2O+Iquu1yz++hy6YAhXjeqbgqnC880dacLqpvR6YhDm4rXNXuPXjcazxC3ywQknjfKR7wuPVvM"
    "6HjE0y11vUR0S9wvHaAuOPiUHd2PcNoS/6O3cwcMRrSFAzYTRwekQhMXTA8Bm/th2NgRPyQHkSNRbzCfK32wmVrzQVf1QUfCXmJ+"
    "V4t9jgS/wQkcdcREO5eHv7CFJ744uIwnbpjs4IkrEkL60RmJhTzxRgKeH/1xlIK44qiJz4Nfs5p4IpWgjHxNPBTUCdPgLEZPpGFf"
    "1H2xTRRqERBExRnbjQpvbGdTdxxQBzH6IlEKRD0Gghh9MfVpgDIUtslMLRYCEE9MUp8iFDa2BOKLY/IDlVgIQHwx2TiPh40yUAuI"
    "AKMnEvMhjYmjIjiVgQLWclDAMgttxMMiD22fWXXJdnPik+QcBizcstETiV+OpkISIFMoZT1KthlnESbbuxXnbB4hS+9sjCBpeprt"
    "T0LmqIAs4mW7zeikhHCyHjDbbUZXHeFW1WgJioTLNNNV1E9HgqhqwARVcdL2fp6otjdLH22z+SJeNtqoiou28hEXHffQ9XDZPFVX"
    "XVTX89V2QeGljTy6zFjbyaOb0oNEE0fNOKBHX6Xy0NBJahrirVlRU3psI4+pemyzlSEeS53KjD47ks/QIJrtM+m2puK2rUyF27b7"
    "TIZTMLVy0lK3HZG3o8uOTLMVj233mCwpbd1p281Gp6VEtNXQCpa4bfYI4rrjOWVJnjsax5Vu296ux1ZHfJZUs0VYbe8Sj80EdLnT"
    "tjfr5SW4an3ZjlQ8t92pTHSb2564LrWvH/125KUnbjtSwxOPHWX5/fZXA9zc7B8er9f39/vvXp2O7//LPxm7ZufHD8ebMHIeO2Xn"
    "h8369nxoUZ1/d9ieNuR6/7jZrU/kxs39/kgnPDysH4dLSLeDbDvIt4NsO8i3e3v4drjCdHPMNsd8c8w2l+l6ma2X+XpZrE+Ek4lw"
    "8vzu6bT5frhW6cNU9jBVGvbxdFjfbM4j9c+/3Xy/uTnvHTwbhWQUs1FMRmU2KpNRlY2qbPR4I8ah4w2cUwcKN5DMlOcDmcOV6kea"
    "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"
    "gQV92P3E7uQgz7YXte0/hx7f/wfwYmH2dbMBAA=="
)


def _materialize_embedded_data() -> Path:
    """Decode the embedded policy-layer fixture into a temporary directory.

    The verifier grades in a separate container that does not mount the agent's
    /app/data tree, so the exact bytes of every policy-layer input the grader
    reads are shipped inside this test module as a gzip+base64 blob. They are
    unpacked once, at import time, into a private temp directory that is removed
    when the process exits.
    """
    packed = gzip.decompress(base64.b64decode("".join(_EMBEDDED_DATA_B64.split())))
    root = Path(tempfile.mkdtemp(prefix="verifier_data_"))
    atexit.register(_cleanup_embedded_data, root)
    offset = 0
    (count,) = struct.unpack_from("<I", packed, offset)
    offset += 4
    for _ in range(count):
        (name_len,) = struct.unpack_from("<I", packed, offset)
        offset += 4
        name = packed[offset : offset + name_len].decode("utf-8")
        offset += name_len
        (data_len,) = struct.unpack_from("<I", packed, offset)
        offset += 4
        data = packed[offset : offset + data_len]
        offset += data_len
        (root / name).write_bytes(data)
    return root


def _cleanup_embedded_data(root: Path) -> None:
    for child in root.iterdir():
        try:
            child.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _resolve_data_dir() -> Path:
    """Locate the policy-layer inputs.

    Prefer an explicit override, then the agent path when it is present (combined
    mode or local runs), and otherwise fall back to the fixture embedded in this
    module so separate-mode grading always has the exact input bytes.
    """
    override = os.environ.get("VERIFIER_DATA_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    agent_data = Path("/app/data")
    if agent_data.is_dir():
        return agent_data
    return _materialize_embedded_data()


DATA = _resolve_data_dir()

FORMAT_TAG = "TASK"
PACKAGE = "task"
TRAILER = "harbor:cli-config:TASK:000000002e552977"
EVAL_SEED = 7439359137350358359
MAX_RECORDS_BASE = 235
RETRY_LIMIT_BASE = 6
TIMEOUT_MS_BASE = 2359

ADOPTED = "ADOPTED"
GRAN_RATE = 25
UID_FLOOR = 1000
QUORUM_PERMILLE = 667
RET_FLOOR, RET_CEIL = 7, 400
BASELINE_COMPLEMENT = 100
RISK_DIVISOR = 1000

_UNTRUSTED = ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups"]
_TOOL_ROOT = None

ARTIFACT_NAMES = [
    "config.json", "report.json", "tool.zip", "validation_report.json",
    "summary.json", "profile_canary.json", "profile_enforce.json",
    "profile_delta_report.json", "admission_matrix.csv", "rate_limit_plan.ini",
    "scope_reconciliation.tsv", "hardening_fold_trace.psv", "quorum_plan.kv",
    "risk_ledger.env", "precedence_resolution.properties", "privilege_map.ndjson",
    "seccomp_summary.tsv", "tls_floor.env", "retention_schedule.csv",
    "derivation_audit.xml", "convergence_report.txt", "reproducibility_manifest.json",
    "input_census.ndjson", "class_ordering.tsv",
]


# ---------- integer model (mirrors the reference implementation) ----------

def round_half_even(num: int, den: int) -> int:
    if den < 0:
        num, den = -num, -den
    q, r = divmod(num, den)
    twice = 2 * r
    if twice < den:
        return q
    if twice > den:
        return q + 1
    return q if q % 2 == 0 else q + 1


def quantize_nearest(value: int, step: int) -> int:
    return round_half_even(value, step) * step


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def read_text(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8", errors="replace")


def read_json(name: str) -> dict:
    return json.loads(read_text(name))


def content_lines(name: str) -> list[str]:
    out = []
    for line in read_text(name).split("\n"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        out.append(line)
    return out


def delimited_rows(name: str, sep: str) -> list[dict]:
    lines = content_lines(name)
    if not lines:
        return []
    header = [c.strip() for c in lines[0].split(sep)]
    rows = []
    for line in lines[1:]:
        parts = [c.strip() for c in line.split(sep)]
        if len(parts) != len(header):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def adopted_rows(rows: list[dict], klass: int) -> list[dict]:
    out = []
    for r in rows:
        if r.get("status") != ADOPTED:
            continue
        cid = r.get("class_id")
        if cid is None or str(cid).strip() != str(klass):
            continue
        out.append(r)
    return out


def section_fields(filename: str, section_name: str) -> dict:
    current = None
    out: dict = {}
    for line in content_lines(filename):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            continue
        if current != section_name:
            continue
        key, sep, val = s.partition("=")
        if not sep:
            continue
        out[key.strip()] = val.strip().strip('"')
    return out


def base_inputs():
    base = read_json("defaults.json")["base_config"]
    return int(base["max_records"]), int(base["retry_limit"]), int(base["timeout_ms"]), str(base["trailer"])


def active_class(max_records: int, retry_limit: int) -> int:
    return ((max_records // 37) + 3 * retry_limit) % 5


def ingress_qps(klass: int) -> int:
    return int(adopted_rows(delimited_rows("ingress_rate.tsv", "\t"), klass)[0]["ingress_qps"])


def burst_and_window(klass: int):
    fields = section_fields("burst_multiplier.ini", f"class.{klass}")
    return int(fields["burst_multiplier_permille"]), int(fields["window_seconds"])


def granted_scopes(klass: int) -> set:
    base = {r["scope"] for r in adopted_rows([json.loads(l) for l in content_lines("scope_base.jsonl")], klass)}
    overlay = {r["scope"] for r in adopted_rows([json.loads(l) for l in content_lines("scope_overlay.ndjson")], klass)}
    deny = {r["scope"] for r in adopted_rows(delimited_rows("scope_deny.psv", "|"), klass)}
    return (base | overlay) - deny


def scope_uid_map() -> dict:
    out = {}
    for line in content_lines("uid_map.env"):
        key, _, val = line.strip().partition("=")
        if key.startswith("SCOPE_") and val.strip().isdigit():
            out[key[len("SCOPE_"):].lower()] = int(val.strip())
    return out


def hardening_fold(klass: int) -> int:
    rows = [r for r in delimited_rows("baseline_controls.scsv", ";") if str(r.get("class_id")).strip() == str(klass)]
    rows.sort(key=lambda r: int(r["order"]))
    score = 0
    prev = None
    for r in rows:
        if r.get("status") != ADOPTED:
            continue
        w = int(r["weight"])
        score += w
        if int(r["carry_flag"]) == 1 and prev is not None:
            score -= prev
        prev = w
    return score


def hardening_bounds(klass: int):
    fields = section_fields("hardening_bounds.toml", f"class.{klass}")
    return int(fields["floor"]), int(fields["ceil"])


def severity_permille(klass: int) -> int:
    for line in content_lines("severity_weight.kv"):
        parts = line.strip().split(":")
        if len(parts) == 3 and parts[0] == f"class{klass}" and parts[2] == ADOPTED:
            return int(parts[1])
    raise ValueError("no adopted severity")


def healthy_voters(klass: int) -> int:
    count = 0
    for line in content_lines("voter_health.ndjson"):
        obj = json.loads(line)
        if str(obj.get("class_id")) == str(klass) and obj.get("status") == ADOPTED and obj.get("healthy") is True:
            count += 1
    return count


def tls_floor(klass: int) -> int:
    best = None
    for r in adopted_rows(delimited_rows("tls_matrix.csv", ","), klass):
        if r.get("enabled") == "true":
            sc = int(r["score"])
            best = sc if best is None else max(best, sc)
    return best


def seccomp_allowed(klass: int) -> int:
    allow = {r["syscall"] for r in adopted_rows(delimited_rows("seccomp_allow.tsv", "\t"), klass)}
    deny = {r["syscall"] for r in adopted_rows(delimited_rows("seccomp_deny.psv", "|"), klass)}
    return len(allow - deny)


def retention_days(klass: int) -> int:
    fields = section_fields("retention.ini", f"class.{klass}")
    return clamp(int(fields["retention_days"]), RET_FLOOR, RET_CEIL)


def precedence_value(klass: int, field: str) -> int:
    value = int(read_json("defaults.json")["layer_defaults"][field])
    overlays = read_json("layer_overrides.json").get(f"class_{klass}")
    if overlays and field in overlays:
        value = int(overlays[field])
    pinned = read_json("pinned_overrides.json").get(f"class_{klass}")
    if pinned and pinned.get("pinned") is True and field in pinned:
        value = int(pinned[field])
    return value


def expected_config() -> dict:
    max_records, retry_limit, timeout_ms, trailer = base_inputs()
    klass = active_class(max_records, retry_limit)
    permille, window = burst_and_window(klass)
    qps = ingress_qps(klass)
    rate_limit = quantize_nearest(round_half_even(qps * permille, 1000), GRAN_RATE)
    burst_ceiling = round_half_even(rate_limit * window, 1)
    granted = granted_scopes(klass)
    uids = scope_uid_map()
    scope_uids = [uids[s] for s in granted if s in uids]
    privilege_drop_uid = max(UID_FLOOR, min(scope_uids)) if scope_uids else UID_FLOOR
    floor, ceil = hardening_bounds(klass)
    hardening_baseline = quantize_nearest(clamp(hardening_fold(klass), floor, ceil), 5)
    sev = severity_permille(klass)
    risk_index = round_half_even((BASELINE_COMPLEMENT - hardening_baseline) * sev, RISK_DIVISOR)
    voters = healthy_voters(klass)
    quorum = min(max(round_half_even(voters * QUORUM_PERMILLE, 1000), 1), voters)
    retry_backoff = quantize_nearest(round_half_even(timeout_ms * (retry_limit + 1), 8), 10)
    return {
        "active_class": klass,
        "audit_retention_days": retention_days(klass),
        "burst_ceiling": burst_ceiling,
        "checksum": "crc32",
        "control_plane": "admission-broker",
        "effective_scope_count": len(granted),
        "enforcement_profile": "class-" + str(klass),
        "hardening_baseline": hardening_baseline,
        "image_runtime": "oci-runc",
        "ingress_rate_limit_qps": rate_limit,
        "max_concurrent_sessions": precedence_value(klass, "max_concurrent_sessions"),
        "mode": "strict-local",
        "nonce_window_ms": precedence_value(klass, "nonce_window_ms"),
        "privilege_drop_uid": privilege_drop_uid,
        "profile": FORMAT_TAG,
        "quorum_threshold": quorum,
        "retry_backoff_base_ms": retry_backoff,
        "risk_index": risk_index,
        "seccomp_allowed_count": seccomp_allowed(klass),
        "session_ttl_seconds": precedence_value(klass, "session_ttl_seconds"),
        "tls_floor_score": tls_floor(klass),
        "trailer": trailer,
    }


def expected_profile(name: str, config: dict) -> dict:
    klass = config["active_class"]
    deltas = read_json("profile_deltas.json")[name]
    base_ttl = precedence_value(klass, "session_ttl_seconds")
    ttl = quantize_nearest(round_half_even(base_ttl * int(deltas["ttl_scale_permille"]), 1000), 5)
    quorum = min(max(config["quorum_threshold"] + int(deltas["quorum_delta"]), 1), healthy_voters(klass))
    return {
        "profile": name,
        "session_ttl_seconds": ttl,
        "quorum_threshold": quorum,
        "risk_index": config["risk_index"] + int(deltas["risk_delta"]),
        "trailer": config["trailer"],
    }


# ---------- output loaders / isolation ----------

def load_json(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def load_kv(name: str) -> dict:
    out = {}
    for line in (OUT / name).read_text(encoding="utf-8").split("\n"):
        s = line.strip()
        if not s or s[0] in "#!;[":
            continue
        key, sep, val = s.partition("=")
        if sep:
            out[key.strip()] = val.strip()
    return out


def root_only() -> bool:
    return os.geteuid() == 0


def untrusted(cmd: list[str]) -> list[str]:
    return _UNTRUSTED + cmd if root_only() else cmd


def tool_root() -> Path:
    global _TOOL_ROOT
    if _TOOL_ROOT is not None:
        return _TOOL_ROOT
    dest = Path(tempfile.mkdtemp(prefix="tool_"))
    seen = set()
    with zipfile.ZipFile(OUT / "tool.zip") as zf:
        for info in zf.infolist():
            nm = info.filename
            if nm.startswith("/") or ".." in Path(nm).parts or "\\" in nm or nm in seen:
                raise AssertionError("unsafe archive member: " + nm)
            seen.add(nm)
            target = dest / nm
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            target.chmod(0o644)
    for p in dest.rglob("*"):
        if p.is_dir():
            p.chmod(0o777)
    dest.chmod(0o777)
    _TOOL_ROOT = dest
    return dest


def source_text() -> str:
    root = tool_root()
    parts = []
    for name in ("cli.py", "schema.json"):
        p = root / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def run_repaired_cli(args: list[str], cwd: Path):
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    proc = subprocess.run(
        untrusted([sys.executable, "-I", "cli.py", *args]),
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30,
    )
    return proc


# ---------- POSITIVE tests ----------

def test_artifacts_exist():
    """POSITIVE: every required output artifact exists and is non-empty."""
    for name in ARTIFACT_NAMES:
        p = OUT / name
        assert p.is_file(), "missing artifact: " + name
        assert p.stat().st_size > 0, "empty artifact: " + name


def test_config_is_flat_json_object():
    """POSITIVE: config.json is a flat scalar object with the exact 22 lowercase keys."""
    cfg = load_json("config.json")
    exp = expected_config()
    assert isinstance(cfg, dict)
    assert set(cfg.keys()) == set(exp.keys()), "config key set differs"
    for k, v in cfg.items():
        assert k == k.strip().lower(), "key not normalized: " + k
        assert not isinstance(v, (dict, list)), "non-scalar value: " + k


def test_config_identity_fields_match_schema():
    """POSITIVE: config.json carries the schema identity fields exactly."""
    cfg = load_json("config.json")
    exp = expected_config()
    for k in ("profile", "checksum", "control_plane", "image_runtime", "mode"):
        assert cfg[k] == exp[k], "identity field mismatch: " + k


def test_active_class_selection_is_correct():
    """POSITIVE: the active enforcement class is the documented modular selection."""
    max_records, retry_limit, _, _ = base_inputs()
    cfg = load_json("config.json")
    assert cfg["active_class"] == active_class(max_records, retry_limit)
    assert cfg["enforcement_profile"] == "class-" + str(cfg["active_class"])


def test_rate_limit_and_burst_chain_consistent():
    """POSITIVE: ingress rate limit and burst ceiling follow the quantized threshold chain."""
    exp = expected_config()
    cfg = load_json("config.json")
    assert cfg["ingress_rate_limit_qps"] == exp["ingress_rate_limit_qps"]
    assert cfg["ingress_rate_limit_qps"] % GRAN_RATE == 0, "rate limit not on 25-qps grain"
    assert cfg["burst_ceiling"] == exp["burst_ceiling"]


def test_privilege_drop_uid_is_min_granted():
    """POSITIVE: privilege drop uid is the floored minimum uid across reconciled scopes."""
    exp = expected_config()
    cfg = load_json("config.json")
    assert cfg["privilege_drop_uid"] == exp["privilege_drop_uid"]
    assert cfg["privilege_drop_uid"] >= UID_FLOOR
    assert cfg["effective_scope_count"] == exp["effective_scope_count"]


def test_hardening_baseline_uses_carry_fold():
    """POSITIVE: the hardening baseline is the carry-aware fold, clamped and quantized to 5."""
    exp = expected_config()
    cfg = load_json("config.json")
    assert cfg["hardening_baseline"] == exp["hardening_baseline"]
    assert cfg["hardening_baseline"] % 5 == 0, "baseline not on 5-point grain"


def test_risk_and_quorum_thresholds_correct():
    """POSITIVE: risk index and quorum threshold follow the severity and voter formulas."""
    exp = expected_config()
    cfg = load_json("config.json")
    assert cfg["risk_index"] == exp["risk_index"]
    assert cfg["quorum_threshold"] == exp["quorum_threshold"]
    assert cfg["quorum_threshold"] >= 1


def test_precedence_layers_resolved():
    """POSITIVE: session/nonce/session-cap values honor defaults<overlay<pinned precedence."""
    exp = expected_config()
    cfg = load_json("config.json")
    for k in ("session_ttl_seconds", "nonce_window_ms", "max_concurrent_sessions"):
        assert cfg[k] == exp[k], "precedence mismatch: " + k


def test_tls_seccomp_retention_correct():
    """POSITIVE: tls floor, seccomp count, retention and retry backoff are derived correctly."""
    exp = expected_config()
    cfg = load_json("config.json")
    for k in ("tls_floor_score", "seccomp_allowed_count", "audit_retention_days", "retry_backoff_base_ms"):
        assert cfg[k] == exp[k], "derived scalar mismatch: " + k
    assert RET_FLOOR <= cfg["audit_retention_days"] <= RET_CEIL


def test_report_records_real_cli_commands():
    """POSITIVE: report.json records the real init/set/validate/export-report command chain."""
    rep = load_json("report.json")
    assert rep["tool"] == PACKAGE + "-ctl"
    cmds = rep["commands"]
    assert len(cmds) >= 6
    verbs = {c["argv"][0] for c in cmds}
    for v in ("init", "set", "validate", "export-report"):
        assert v in verbs, "missing command: " + v
    assert all(c["returncode"] == 0 for c in cmds), "a recorded command failed"


def test_report_is_deterministic_and_truthful():
    """POSITIVE: report config_sha256 matches a fresh CLI build and carries no timestamps."""
    rep = load_json("report.json")
    root = tool_root()
    run_repaired_cli(["init", "--schema", str(root / "schema.json"), "--out", str(root / "verify.json")], root)
    cfg = expected_config()
    for k in sorted(cfg):
        run_repaired_cli(["set", "--config", str(root / "verify.json"), "--key", k, "--value", str(cfg[k])], root)
    built = json.loads((root / "verify.json").read_text())
    digest = hashlib.sha256((json.dumps(built, indent=2, sort_keys=True) + "\n").encode()).hexdigest()
    assert rep["config_sha256"] == digest, "report digest not reproducible"
    blob = (OUT / "report.json").read_text()
    for banned in ("generated_at", "timestamp", "random_seed"):
        assert banned not in blob, "non-deterministic field present: " + banned


def test_config_round_trips_through_cli_validator():
    """POSITIVE: the repaired CLI validates the derived config against schema.json."""
    root = tool_root()
    (root / "rt.json").write_text(json.dumps(expected_config(), indent=2, sort_keys=True) + "\n")
    proc = run_repaired_cli(["validate", "--schema", str(root / "schema.json"), "--config", str(root / "rt.json")], root)
    assert proc.returncode == 0, "validate failed: " + proc.stderr
    assert proc.stdout.strip() == "ok"


def test_cli_requires_subcommand_and_runs_real_commands():
    """POSITIVE: the repaired CLI rejects a bare invocation and initializes from the schema."""
    root = tool_root()
    bare = run_repaired_cli([], root)
    assert bare.returncode != 0, "bare CLI invocation is accepted; the subcommand is not required"
    seeded_path = root / "init_check.json"
    init = run_repaired_cli(["init", "--schema", str(root / "schema.json"), "--out", str(seeded_path)], root)
    assert init.returncode == 0, "repaired CLI init failed: " + init.stderr
    seeded = json.loads(seeded_path.read_text())
    schema = json.loads((root / "schema.json").read_text())
    assert seeded.get("profile") == schema["profile"], "init did not seed the profile identity from the schema"


def test_profile_canary_matches_reference():
    """POSITIVE: profile_canary.json equals the derived canary profile."""
    assert load_json("profile_canary.json") == expected_profile("canary", expected_config())


def test_profile_enforce_matches_reference():
    """POSITIVE: profile_enforce.json equals the derived enforce profile."""
    assert load_json("profile_enforce.json") == expected_profile("enforce", expected_config())


def test_profile_delta_report_is_correct():
    """POSITIVE: profile_delta_report.json reports signed profile-minus-main differences."""
    cfg = expected_config()
    can = expected_profile("canary", cfg)
    enf = expected_profile("enforce", cfg)
    delta = load_json("profile_delta_report.json")
    for prof, ref in (("canary", can), ("enforce", enf)):
        for k in ("quorum_threshold", "risk_index", "session_ttl_seconds"):
            assert delta[prof][k] == ref[k] - cfg[k], "delta mismatch: " + prof + "." + k


def test_rate_limit_plan_matches_config():
    """POSITIVE: rate_limit_plan.ini echoes the config rate limit and burst ceiling."""
    cfg = load_json("config.json")
    text = (OUT / "rate_limit_plan.ini").read_text()
    assert "ingress_rate_limit_qps = " + str(cfg["ingress_rate_limit_qps"]) in text
    assert "burst_ceiling = " + str(cfg["burst_ceiling"]) in text


def test_hardening_fold_trace_matches_running_score():
    """POSITIVE: hardening_fold_trace.psv terminates at the carry-aware fold score."""
    exp = expected_config()
    klass = exp["active_class"]
    floor, ceil = hardening_bounds(klass)
    lines = [l for l in (OUT / "hardening_fold_trace.psv").read_text().split("\n") if l.strip()]
    assert lines[0].split("|")[0] == "order"
    running = [l.split("|")[-1] for l in lines[1:] if not l.endswith("skip")]
    assert running, "no applied controls in trace"
    assert int(running[-1]) == hardening_fold(klass), "fold trace running total does not match the carry-aware fold"
    assert quantize_nearest(clamp(hardening_fold(klass), floor, ceil), 5) == exp["hardening_baseline"]


def test_quorum_and_risk_ledgers_match_config():
    """POSITIVE: quorum_plan.kv and risk_ledger.env echo the derived thresholds."""
    cfg = load_json("config.json")
    q = {}
    for line in (OUT / "quorum_plan.kv").read_text().split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            q[k.strip()] = v.strip()
    assert int(q["quorum_threshold"]) == cfg["quorum_threshold"]
    risk = load_kv("risk_ledger.env")
    assert int(risk["RISK_INDEX"]) == cfg["risk_index"]
    assert int(risk["HARDENING_BASELINE"]) == cfg["hardening_baseline"]


def test_precedence_resolution_reports_layers():
    """POSITIVE: precedence_resolution.properties resolves each precedence field to config."""
    cfg = load_json("config.json")
    text = (OUT / "precedence_resolution.properties").read_text()
    for f in ("session_ttl_seconds", "nonce_window_ms", "max_concurrent_sessions"):
        assert f + ".resolved=" + str(cfg[f]) in text, "unresolved precedence field: " + f


def test_privilege_and_seccomp_and_tls_sidecars():
    """POSITIVE: privilege_map, seccomp_summary and tls_floor sidecars match the config."""
    cfg = load_json("config.json")
    priv = [json.loads(l) for l in (OUT / "privilege_map.ndjson").read_text().split("\n") if l.strip()]
    assert len(priv) == cfg["effective_scope_count"]
    tls = load_kv("tls_floor.env")
    assert int(tls["TLS_FLOOR_SCORE"]) == cfg["tls_floor_score"]
    secc = (OUT / "seccomp_summary.tsv").read_text()
    assert str(cfg["seccomp_allowed_count"]) in secc


def test_class_ordering_sorted_by_risk():
    """POSITIVE: class_ordering.tsv lists the three profiles in ascending risk order."""
    lines = [l for l in (OUT / "class_ordering.tsv").read_text().split("\n") if l.strip()]
    assert lines[0].split("\t")[0] == "profile"
    vals = [int(l.split("\t")[1]) for l in lines[1:]]
    assert len(vals) == 3
    assert vals == sorted(vals), "profiles not sorted by risk"


def test_validation_report_ok_and_complete():
    """POSITIVE: validation_report.json is ok with all artifacts present and flat config."""
    v = load_json("validation_report.json")
    assert v["ok"] is True
    assert v["all_artifacts_present"] is True
    assert v["config_scalar_flat"] is True
    assert v["command_failures"] == []


def test_summary_lists_inputs_and_artifacts():
    """POSITIVE: summary.json enumerates >=50 inputs, both profiles and 24 artifacts."""
    s = load_json("summary.json")
    assert s["input_count"] >= 50
    assert s["tool"] == PACKAGE + "-ctl"
    assert set(s["profiles"]) == {"canary", "enforce"}
    assert s["artifact_count"] == len(ARTIFACT_NAMES)


def test_reproducibility_manifest_is_stable():
    """POSITIVE: reproducibility_manifest.json pins the config digest and key count."""
    rep = load_json("report.json")
    man = load_json("reproducibility_manifest.json")
    assert man["config_sha256"] == rep["config_sha256"]
    assert man["key_count"] == len(expected_config())
    assert man["format_tag"] == FORMAT_TAG


def test_input_census_covers_data_layers():
    """POSITIVE: input_census.ndjson records every data file with row counts."""
    census = [json.loads(l) for l in (OUT / "input_census.ndjson").read_text().split("\n") if l.strip()]
    assert len(census) >= 50
    names = {c["name"] for c in census}
    for needed in ("ingress_rate.tsv", "baseline_controls.scsv", "voter_health.ndjson", "defaults.json"):
        assert needed in names, "census missing data layer: " + needed
    assert all(c["rows"] > 0 for c in census)


def test_outputs_are_world_readable_regular_files():
    """POSITIVE: all artifacts are regular world-readable files."""
    for name in ARTIFACT_NAMES:
        st = (OUT / name).stat()
        assert stat.S_ISREG(st.st_mode), "not a regular file: " + name
        assert st.st_mode & stat.S_IROTH, "not world-readable: " + name


def test_canonical_trailer_present():
    """POSITIVE: the canonical trailer appears exactly once in config.json."""
    text = (OUT / "config.json").read_text()
    assert text.count(TRAILER) == 1


# ---------- NEGATIVE tests (polarity inverted) ----------

def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: fires only if derived answers were hardcoded into the CLI source."""
    src = source_text()
    exp = expected_config()
    needles = [str(EVAL_SEED), str(exp["hardening_baseline"]), str(exp["risk_index"]), str(exp["ingress_rate_limit_qps"])]
    hits = [n for n in needles if re.search(r"(?<!\d)" + re.escape(n) + r"(?!\d)", src)]
    assert hits, "no hardcoded answers detected"


def test_negative_weight_nested_or_dynamic_config():
    """NEGATIVE: fires only if config.json is nested or carries dynamic keys."""
    cfg = load_json("config.json")
    dynamic = {"timestamp", "generated_at", "uuid", "random_seed", "admission", "ladder"}
    nested = any(isinstance(v, (dict, list)) for v in cfg.values())
    assert nested or (set(cfg) & dynamic), "config is flat and static"


def test_negative_weight_fake_cli_transcript():
    """NEGATIVE: fires only if report claims commands the CLI cannot actually run."""
    rep = load_json("report.json")
    claims = len(rep.get("commands", [])) >= 6
    root = tool_root()
    (root / "chk.json").write_text(json.dumps(expected_config(), indent=2, sort_keys=True) + "\n")
    proc = run_repaired_cli(["validate", "--schema", str(root / "schema.json"), "--config", str(root / "chk.json")], root)
    broken = (not (root / "cli.py").exists()) or proc.returncode != 0
    assert claims and broken, "cli transcript is genuine"


def test_negative_weight_network_or_secret_material():
    """NEGATIVE: fires only if source or config imports network libs or secrets."""
    blob = source_text().lower() + (OUT / "config.json").read_text().lower() + (OUT / "report.json").read_text().lower()
    markers = ("import requests", "import urllib", "import socket", "https://", "api_key", "password")
    assert any(m in blob for m in markers), "no network or secret material present"


if __name__ == "__main__":
    failures = []
    for _name in sorted(n for n in dict(globals()) if n.startswith("test_")):
        try:
            globals()[_name]()
        except Exception as exc:  # noqa: BLE001
            failures.append(_name + ": " + repr(exc))
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("all tests passed")
