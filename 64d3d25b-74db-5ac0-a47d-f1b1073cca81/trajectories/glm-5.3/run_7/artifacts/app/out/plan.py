#!/usr/bin/env python3
"""Derive the installation dossier from an exhibition snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

The snapshot under DATA_DIR is treated as read only. The twenty four derived
artifacts are written into OUT_DIR. All arithmetic is integer or exact
rational; no floating point value is produced, compared or written. Two runs
over the same input bytes produce identical output bytes.
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

# ------------------------------------------------------------------ constants

LEN_FACTORS = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTORS = {"G": 1, "KG": 1000}
MM = 1000                       # millimetre margin / interior factor
SECONDS_PER_DAY = 3600
DEFAULT_SETUP_DIV = 700
DEFAULT_CHAIN_SEED = "0" * 64

# Dependency kinds. Removal precedence ascends: a bay size edge is removed
# before a conservation edge, which is removed before an explicit rule.
KIND_ORDER = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}

# Artifacts hashed into the integrity manifest: every jsonl artifact, the
# penalty summary, the objective tuple and this program file.
HASHED_NAMES = [
    "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl",
    "canonical_objects.jsonl",
    "conservation_adjusted.jsonl",
    "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl",
    "crate_candidate_matrix.jsonl",
    "crate_contention_report.jsonl",
    "crew_requirement_ledger.jsonl",
    "cycle_resolution_audit.jsonl",
    "dependency_edges.jsonl",
    "deterministic_audit.jsonl",
    "gallery_occupancy_map.jsonl",
    "install_order.jsonl",
    "install_window_table.jsonl",
    "normalized_geometry.jsonl",
    "objective_tuple.json",
    "penalty_summary.json",
    "rejected_placement.jsonl",
    "selected_placement.jsonl",
    "transport_leg_manifest.jsonl",
    "unschedulable_report.jsonl",
]


# ------------------------------------------------------------------ helpers

def r_half_up(num, den):
    """Round the exact rational num/den half up to an integer."""
    if den == 0:
        return 0
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def ratio_pair(num, den):
    """Reduced integer pair; a zero denominator yields the sentinel 0/1."""
    if den == 0:
        return 0, 1
    f = Fraction(num, den)
    return f.numerator, f.denominator


def cells(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ival(text):
    return int(str(text).strip())


def sval(text):
    return str(text).strip()


def truthy(text):
    """Parse a booleanish csv cell without inventing a value."""
    t = str(text).strip().lower()
    if t in ("1", "true", "yes", "y", "t"):
        return 1
    if t in ("0", "false", "no", "n", "f", ""):
        return 0
    digits = ""
    for ch in t:
        if ch.isdigit() or (ch in "+-" and not digits):
            digits += ch
        else:
            break
    return 1 if digits and int(digits) != 0 else 0


def class_rank(code):
    """Conservation class sensitivity rank; higher means more sensitive."""
    text = str(code).strip()
    digits = ""
    for ch in reversed(text):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    if digits:
        return (1, int(digits), text)
    return (0, 0, text)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")


def write_json_obj(path, value):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ geometry

def oriented(obj, index):
    """Footprint width, footprint depth and vertical extent of an orientation."""
    cw, cd, ch = obj["_cw"], obj["_cd"], obj["_ch"]
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def fits_bay(obj, index, bay):
    """Width inclusive, depth exclusive, height inclusive."""
    fw, fd, vh = oriented(obj, index)
    return fw <= bay["_w"] and fd < bay["_d"] and vh <= bay["_h"]


# ------------------------------------------------------------------ graphs

def strongly_connected(nodes, adj):
    """Iterative Tarjan; returns a list of components (each a list of nodes)."""
    index = {}
    low = {}
    on_stack = {}
    stack = []
    result = []
    counter = [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
                elif on_stack.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
    return result


def break_cycles(edges, nodes):
    """Deterministically remove one cycle edge at a time.

    The edge removed is the lowest priority edge lying on a cycle under the
    ordering kind, then weight, then from id, then to id. An edge lies on a
    cycle exactly when its ends share a non trivial strongly connected
    component, or when it is a self loop.
    """
    removed = []
    live = dict(edges)

    def scan():
        adj = {}
        for (u, v) in live:
            adj.setdefault(u, []).append(v)
        comps = strongly_connected(sorted(nodes), adj)
        comp_of = {}
        for i, comp in enumerate(comps):
            for n in comp:
                comp_of[n] = i
        cyclic_comps = {i for i, comp in enumerate(comps) if len(comp) > 1}
        on_cycle = set()
        for (u, v) in live:
            if u == v or (comp_of[u] == comp_of[v] and comp_of[u] in cyclic_comps):
                on_cycle.add((u, v))
        return comps, cyclic_comps, on_cycle

    while True:
        comps, cyclic_comps, on_cycle = scan()
        if not on_cycle:
            break
        candidate = None
        for (u, v) in on_cycle:
            kind, weight = live[(u, v)]
            key = (KIND_ORDER[kind], weight, u, v)
            if candidate is None or key < candidate[0]:
                candidate = (key, (u, v), (kind, weight))
        if candidate is None:
            break  # defensive: a cycle always carries at least one edge
        (u, v) = candidate[1]
        (kind, weight) = candidate[2]
        del live[(u, v)]
        removed.append({"from": u, "kind": kind, "to": v, "weight": weight})

    comps, cyclic_comps, on_cycle = scan()
    final_cycles = len(cyclic_comps) + len([1 for (u, v) in live if u == v])
    return live, removed, final_cycles


def install_order(nodes, edges, setup):
    """Stage 12: repeatedly take the ready object with least setup seconds."""
    preds = {n: 0 for n in nodes}
    succ = {n: [] for n in nodes}
    for (u, v) in edges:
        preds[v] += 1
        succ[u].append(v)
    ready = sorted(n for n in nodes if preds[n] == 0)
    order = []
    while ready:
        best = min(ready, key=lambda n: (setup.get(n, 0), n))
        ready.remove(best)
        order.append(best)
        for v in succ[best]:
            preds[v] -= 1
            if preds[v] == 0:
                ready.append(v)
    return order


# ------------------------------------------------------------------ stages

def derive(data_dir, out_dir):
    csv_names = [
        "bay_exceptions.csv", "conservation_rules.csv", "crate_catalog.csv",
        "crate_reservations.csv", "crew_roster.csv", "dependency_rules.csv",
        "gallery_bays.csv", "handling_class_rules.csv", "lender_master.csv",
        "material_rules.csv", "object_dimensions.csv", "object_master.csv",
        "object_mass.csv", "object_orientation.csv", "object_value.csv",
        "object_version_map.csv", "prior_state.csv", "transport_rules.csv",
        "value_class_rules.csv", "venue_windows.csv",
    ]
    raw = {name: cells(os.path.join(data_dir, name)) for name in csv_names}
    horizon = load_json(os.path.join(data_dir, "horizon_config.json"))
    objective_cfg = load_json(os.path.join(data_dir, "objective_order.json"))
    setup_div = ival(horizon.get("setup_div", DEFAULT_SETUP_DIV)) or DEFAULT_SETUP_DIV
    chain_seed = str(horizon.get("chain_seed", DEFAULT_CHAIN_SEED))
    # The unit the handling class margins are declared in, and the end
    # conventions of the two windows that are not simply inclusive.
    margin_unit = sval(horizon.get("clearance_unit", "MM")) or "MM"
    margin_factor = LEN_FACTORS.get(margin_unit, MM)
    venue_end_exclusive = sval(horizon.get("venue_end_convention")) == "exclusive"
    cons_end_exclusive = sval(horizon.get("conservation_end_convention")) == "exclusive"

    # ---- reference tables
    material = {sval(r["material_code"]): r for r in raw["material_rules.csv"]}
    handling = {sval(r["handling_class"]): r for r in raw["handling_class_rules.csv"]}
    cons_rules = {}
    for r in raw["conservation_rules.csv"]:
        cons_rules[sval(r["conservation_class"])] = {
            "start": ival(r["setup_start"]),
            "end": ival(r["setup_end"]),
            "needs_dark": truthy(r["needs_dark"]),
        }
    lenders = {}
    for r in raw["lender_master.csv"]:
        lenders[sval(r["lender_id"])] = {
            "num": ival(r["insurance_rate_num"]),
            "den": ival(r["insurance_rate_den"]),
        }
    value_classes = []
    for r in raw["value_class_rules.csv"]:
        value_classes.append({
            "value_class": sval(r["value_class"]),
            "min_value": ival(r["min_value"]),
            "unschedulable_penalty": ival(r["unschedulable_penalty"]),
            "contention_penalty": ival(r["contention_penalty"]),
        })
    # Highest class first; the staged threshold comparisons run descending.
    value_classes.sort(key=lambda c: (-c["min_value"], c["value_class"]))

    def value_class_of(value_declared):
        for vc in value_classes:
            if value_declared >= vc["min_value"]:
                return vc
        return value_classes[-1] if value_classes else None

    # ---- stage 1: canonical identity and units
    dims = {sval(r["object_id"]): r for r in raw["object_dimensions.csv"]}
    masses = {sval(r["object_id"]): r for r in raw["object_mass.csv"]}
    values = {sval(r["object_id"]): ival(r["declared_value"]) for r in raw["object_value.csv"]}
    versions = {}
    for r in raw["object_version_map.csv"]:
        oid = sval(r["object_id"])
        v = ival(r["version"])
        if oid not in versions or v > versions[oid]:
            versions[oid] = v

    objects = []
    seen = set()
    for r in raw["object_master.csv"]:
        oid = sval(r["object_id"])
        if oid in seen:
            continue                       # stale duplicate row, first wins
        seen.add(oid)
        d = dims[oid]
        factor = LEN_FACTORS[sval(d["dim_unit"])]
        w = ival(d["width"]) * factor
        h = ival(d["height"]) * factor
        dp = ival(d["depth"]) * factor
        m = masses[oid]
        mass_g = ival(m["mass"]) * MASS_FACTORS[sval(m["mass_unit"])]
        mat = material[sval(r["material_code"])]
        hc = handling[sval(mat["handling_class"])]
        obj = {
            "object_id": oid,
            "lender_id": sval(r["lender_id"]),
            "material_code": sval(r["material_code"]),
            "version": versions.get(oid, 0),
            "declared_value_cents": values.get(oid, 0) * 100,
            "_declared_value": values.get(oid, 0),
            "mass_g": mass_g,
            "_w": w, "_h": h, "_d": dp,
            "_handling_class": sval(mat["handling_class"]),
            "_cons_class": sval(mat["conservation_class"]),
            "_cons": cons_rules[sval(mat["conservation_class"])],
            "_max_lux": ival(mat["max_lux"]),
            "_crew_class": sval(hc["crew_class"]),
            "_setup_base": ival(hc["setup_base"]),
        }
        objects.append(obj)
    by_id = {o["object_id"]: o for o in objects}

    # ---- stage 2: normalized geometry
    for obj in objects:
        hc = handling[obj["_handling_class"]]
        mw = ival(hc["clearance_w"]) * margin_factor
        md = ival(hc["clearance_d"]) * margin_factor
        mh = ival(hc["clearance_h"]) * margin_factor
        obj["_cw"] = obj["_w"] + 2 * mw
        obj["_cd"] = obj["_d"] + 2 * md
        obj["_ch"] = obj["_h"] + mh
        obj["_handle_margin"] = ival(hc["handle_margin"]) * margin_factor
        num, den = ratio_pair(obj["_h"], obj["_w"])
        obj["aspect_num"], obj["aspect_den"] = num, den
        # handling envelope: the margin on all three axes
        obj["_hw"] = obj["_w"] + 2 * obj["_handle_margin"]
        obj["_hh"] = obj["_h"] + 2 * obj["_handle_margin"]
        obj["_hd"] = obj["_d"] + 2 * obj["_handle_margin"]
        obj["_volume"] = obj["_hw"] * obj["_hh"] * obj["_hd"]

    # ---- stage 3: orientation admissibility
    orient_rows = {sval(r["object_id"]): r for r in raw["object_orientation.csv"]}
    for obj in objects:
        row = orient_rows.get(obj["object_id"])
        tip = truthy(row["tip_allowed"]) if row else 0
        rot = truthy(row["rotate_allowed"]) if row else 0
        obj["_tip"] = tip
        obj["_rot"] = rot
        orientations = [0]
        if rot:
            orientations.append(1)
        if tip:
            orientations.extend([2, 3])
        obj["orientations"] = orientations
        obj["tip_allowed"] = tip
        obj["rotate_allowed"] = rot

    # ---- bays
    bays = []
    for r in raw["gallery_bays.csv"]:
        bays.append({
            "bay_id": sval(r["bay_id"]),
            "_w": ival(r["interior_w"]) * MM,
            "_h": ival(r["interior_h"]) * MM,
            "_d": ival(r["interior_d"]) * MM,
            "_lux": ival(r["lux_level"]),
            "_priority": ival(r["bay_priority"]),
        })
    bays_by_id = sorted(bays, key=lambda b: b["bay_id"])
    bays_by_scan = sorted(bays, key=lambda b: (b["_priority"], b["bay_id"]))
    blocked = {}
    for r in raw["bay_exceptions.csv"]:
        blocked.setdefault(sval(r["bay_id"]), set()).add(sval(r["blocked_class"]))

    # ---- stage 4: bay candidate matrix
    bay_rows = []
    for obj in objects:
        for bay in bays_by_id:
            best = None
            for idx in obj["orientations"]:
                if fits_bay(obj, idx, bay):
                    best = idx
                    break
            bay_rows.append({
                "object_id": obj["object_id"],
                "bay_id": bay["bay_id"],
                "fits": 1 if best is not None else 0,
                "orientation": -1 if best is None else best,
            })

    # ---- stage 5: selected placement (geometry only)
    selected_rows = []
    for obj in objects:
        chosen = None
        for bay in bays_by_scan:
            for idx in obj["orientations"]:
                if fits_bay(obj, idx, bay):
                    chosen = (bay, idx)
                    break
            if chosen:
                break
        if chosen is None:
            code = "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"
            selected_rows.append({
                "object_id": obj["object_id"], "bay_id": "", "orientation": -1,
                "code": code,
            })
            obj["_geom_bay"] = None
            obj["_geom_idx"] = -1
            obj["_geom_code"] = code
        else:
            selected_rows.append({
                "object_id": obj["object_id"], "bay_id": chosen[0]["bay_id"],
                "orientation": chosen[1], "code": "P_OK",
            })
            obj["_geom_bay"] = chosen[0]
            obj["_geom_idx"] = chosen[1]
            obj["_geom_code"] = "P_OK"

    # ---- stage 6: conservation adjustment
    def cons_ok(obj, bay):
        if bay["_lux"] > obj["_max_lux"]:
            return False
        if obj["_cons_class"] in blocked.get(bay["bay_id"], ()):
            return False
        if obj["_cons"]["needs_dark"] and bay["_lux"] > 0:
            return False
        return True

    cons_rows = []
    for obj in objects:
        chosen = None
        for bay in bays_by_scan:
            if not cons_ok(obj, bay):
                continue
            for idx in obj["orientations"]:
                if fits_bay(obj, idx, bay):
                    chosen = (bay, idx)
                    break
            if chosen:
                break
        if chosen is None:
            if obj["_geom_bay"] is None:
                code = "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"
            else:
                code = "P_CONSERVATION_BLOCK"
            cons_rows.append({
                "object_id": obj["object_id"], "bay_id": "", "orientation": -1,
                "code": code,
            })
            obj["_bay"] = None
            obj["_orient"] = -1
            obj["_cons_code"] = code
        else:
            cons_rows.append({
                "object_id": obj["object_id"], "bay_id": chosen[0]["bay_id"],
                "orientation": chosen[1], "code": "P_OK",
            })
            obj["_bay"] = chosen[0]
            obj["_orient"] = chosen[1]
            obj["_cons_code"] = "P_OK"

    # every object that did not end the placement stages in a bay
    rejected_rows = [{"object_id": obj["object_id"], "code": obj["_cons_code"]}
                     for obj in objects if obj["_cons_code"] != "P_OK"]

    # ---- stage 7: crate candidates
    crates = []
    for r in raw["crate_catalog.csv"]:
        w = ival(r["inner_w"]) * MM
        h = ival(r["inner_h"]) * MM
        d = ival(r["inner_d"]) * MM
        crates.append({
            "crate_class": sval(r["crate_class"]),
            "_dims": tuple(sorted((w, h, d))),
            "_volume": w * h * d,
            "_count": ival(r["count"]),
            "_mode": sval(r["transport_mode"]),
        })
    crates.sort(key=lambda c: c["crate_class"])

    crate_cand_rows = []
    for obj in objects:
        hd = sorted((obj["_hw"], obj["_hh"], obj["_hd"]))
        fitting = []
        for crate in crates:
            cdim = crate["_dims"]
            if hd[0] <= cdim[0] and hd[1] <= cdim[1] and hd[2] <= cdim[2]:
                residual = crate["_volume"] - obj["_volume"]
                fitting.append((residual, crate["crate_class"], crate))
                crate_cand_rows.append({
                    "object_id": obj["object_id"],
                    "crate_class": crate["crate_class"],
                    "residual": residual,
                })
        fitting.sort(key=lambda t: (t[0], t[1]))
        obj["_crates"] = fitting
        obj["_primary_crate"] = fitting[0][1] if fitting else ""
    crate_cand_rows.sort(key=lambda r: (r["object_id"], r["residual"], r["crate_class"]))

    # ---- stage 13 prerequisites: crew class and setup seconds
    setup = {}
    crew_rows = []
    for obj in objects:
        seconds = r_half_up(obj["_setup_base"] * obj["mass_g"], setup_div)
        obj["_setup_seconds"] = seconds
        setup[obj["object_id"]] = seconds
        crew_rows.append({
            "object_id": obj["object_id"],
            "crew_class": obj["_crew_class"],
            "setup_seconds": seconds,
        })

    # ---- stages 10 to 12: dependency graph, cycles, install order
    edges = {}

    def add_edge(u, v, kind, weight):
        key = (u, v)
        cur = edges.get(key)
        if cur is None or KIND_ORDER[kind] > KIND_ORDER[cur[0]]:
            edges[key] = (kind, weight)

    for r in raw["dependency_rules.csv"]:
        u = sval(r["from_object"])
        v = sval(r["to_object"])
        if u in by_id and v in by_id:
            add_edge(u, v, "D_RULE", ival(r["edge_priority"]))

    bay_members = {}
    for obj in objects:
        if obj["_bay"] is not None:
            bay_members.setdefault(obj["_bay"]["bay_id"], []).append(obj)
    for bay_id in sorted(bay_members):
        members = bay_members[bay_id]
        by_size = sorted(members, key=lambda o: (-o["_volume"], o["object_id"]))
        for i in range(len(by_size)):
            for j in range(i + 1, len(by_size)):
                early = by_size[i]
                late = by_size[j]
                add_edge(early["object_id"], late["object_id"], "D_BAYSIZE",
                         early["_volume"])
        # most sensitive first; equal sensitivity keeps ascending object id
        # (reverse=True preserves the stable order of equal keys)
        by_sens = sorted(members, key=lambda o: o["object_id"])
        by_sens = sorted(by_sens, key=lambda o: class_rank(o["_cons_class"]),
                         reverse=True)
        for i in range(len(by_sens)):
            for j in range(len(by_sens)):
                if i == j:
                    continue
                early = by_sens[i]
                late = by_sens[j]
                if class_rank(early["_cons_class"]) <= class_rank(late["_cons_class"]):
                    continue   # only a strictly more sensitive class leads
                add_edge(early["object_id"], late["object_id"], "D_CONSERVATION",
                         early["_volume"])

    edge_rows = [{
        "from": u, "to": v, "kind": edges[(u, v)][0], "weight": edges[(u, v)][1],
    } for (u, v) in sorted(edges)]

    node_ids = [o["object_id"] for o in objects]
    live_edges, removed_edges, final_cycles = break_cycles(edges, node_ids)

    order = install_order(node_ids, live_edges, setup)
    position = {oid: i + 1 for i, oid in enumerate(order)}
    install_rows = [{"position": i + 1, "object_id": oid}
                    for i, oid in enumerate(order)]

    # ---- stage 8: shared crate capacity, consumed in install order
    reservations = {}
    for r in raw["crate_reservations.csv"]:
        key = (sval(r["crate_class"]), sval(r["lender_id"]))
        reservations[key] = reservations.get(key, 0) + ival(r["reserved_count"])
    prior = {}
    for r in raw["prior_state.csv"]:
        prior[sval(r["crate_class"])] = prior.get(sval(r["crate_class"]), 0) \
            + ival(r["consumed_count"])
    remaining = {c["crate_class"]: c["_count"] - prior.get(c["crate_class"], 0)
                 for c in crates}
    res_left = dict(reservations)

    assignment = {}
    for oid in order:
        obj = by_id[oid]
        if obj["_bay"] is None:
            continue                      # never placed, claims no crate
        best = None
        for residual, crate_class, crate in obj["_crates"]:
            if remaining.get(crate_class, 0) <= 0:
                continue
            reserved = res_left.get((crate_class, obj["lender_id"]), 0)
            key = (0 if reserved > 0 else 1, residual, crate_class)
            if best is None or key < best[0]:
                best = (key, crate_class)
        if best is None:
            assignment[oid] = None
            continue
        crate_class = best[1]
        remaining[crate_class] -= 1
        if res_left.get((crate_class, obj["lender_id"]), 0) > 0:
            res_left[(crate_class, obj["lender_id"])] -= 1
        assignment[oid] = crate_class

    crate_rows = []
    contention_rows = []
    displaced_ids = []
    for obj in sorted(objects, key=lambda o: o["object_id"]):
        oid = obj["object_id"]
        got = assignment.get(oid)
        primary = obj["_primary_crate"]
        if got is None:
            code = "C_NO_CRATE"
        elif got == primary:
            code = "C_PRIMARY"
        else:
            code = "C_DISPLACED"
            displaced_ids.append(oid)
            contention_rows.append({
                "object_id": oid, "wanted_class": primary, "assigned_class": got,
            })
        obj["_crate"] = got
        crate_rows.append({
            "object_id": oid, "crate_class": got if got is not None else "",
            "code": code, "primary_class": primary,
        })
    ledger_rows = []
    for crate in crates:
        cc = crate["crate_class"]
        ledger_rows.append({
            "crate_class": cc,
            "catalog_count": crate["_count"],
            "prior_consumed": prior.get(cc, 0),
            "remaining": remaining[cc],
        })

    # ---- stage 9: transport legs
    fallback = {}
    for r in raw["transport_rules.csv"]:
        fallback[sval(r["lender_id"])] = (sval(r["forbidden_mode"]),
                                          sval(r["fallback_mode"]))
    transport_rows = []
    for obj in objects:
        oid = obj["object_id"]
        got = obj["_crate"]
        if got is None:
            continue
        crate = next(c for c in crates if c["crate_class"] == got)
        mode = crate["_mode"]
        exc = 0
        rule = fallback.get(obj["lender_id"])
        if rule and rule[0] == mode:
            mode = rule[1]
            exc = 1
        obj["_mode"] = mode
        obj["_mode_exception"] = exc
        transport_rows.append({
            "object_id": oid, "crate_class": got, "mode": mode, "exception": exc,
        })

    # ---- stage 13: install windows
    crew_roster = {}
    for r in raw["crew_roster.csv"]:
        crew_roster[sval(r["crew_class"])] = (ival(r["avail_start"]),
                                              ival(r["avail_end"]))
    venue = {}
    for r in raw["venue_windows.csv"]:
        venue[sval(r["bay_id"])] = (ival(r["access_start"]), ival(r["access_end"]))

    window_rows = []
    unsched_rows = []
    for obj in objects:
        oid = obj["object_id"]
        if obj["_bay"] is None:
            code = "S_NO_PLACEMENT"
            bay_id, wstart, wend = "", "", ""
            req = -(-obj["_setup_seconds"] // SECONDS_PER_DAY)
        else:
            bay_id = obj["_bay"]["bay_id"]
            cstart = obj["_cons"]["start"]
            cend = obj["_cons"]["end"]
            if cons_end_exclusive:
                cend -= 1
            vstart, vend = venue.get(bay_id, (0, 0))
            if venue_end_exclusive:
                vend -= 1               # an exclusive access end, minus one
            astart, aend = crew_roster.get(obj["_crew_class"], (0, 0))
            low = max(cstart, vstart, astart)
            high = min(cend, vend, aend)
            days_available = high - low + 1
            req = -(-obj["_setup_seconds"] // SECONDS_PER_DAY)
            if obj["_crate"] is None:
                code = "S_NO_CRATE"
            elif days_available <= 0:
                code = "S_NO_WINDOW"
            elif req > days_available:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
            if days_available <= 0:
                wstart, wend = "", ""
            else:
                wstart, wend = low, high
        obj["_window_start"] = wstart
        obj["_window_end"] = wend
        obj["_required_days"] = req
        obj["_sched_code"] = code
        obj["_scheduled"] = 1 if code == "S_OK" else 0
        window_rows.append({
            "object_id": oid, "bay_id": bay_id, "window_start": wstart,
            "window_end": wend, "required_days": req, "code": code,
        })
        if code != "S_OK":
            unsched_rows.append({"object_id": oid, "code": code})
    unsched_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 14: dossier
    occupancy_rows = []
    for bay in bays_by_id:
        members = sorted(o["object_id"] for o in objects
                         if o["_bay"] is not None
                         and o["_bay"]["bay_id"] == bay["bay_id"])
        occupancy_rows.append({
            "bay_id": bay["bay_id"],
            "object_count": len(members),
            "objects": members,
        })

    unsched_penalty = 0
    for row in unsched_rows:
        obj = by_id[row["object_id"]]
        vc = value_class_of(obj["_declared_value"])
        if vc:
            unsched_penalty += vc["unschedulable_penalty"]
    contention_penalty = 0
    for oid in displaced_ids:
        obj = by_id[oid]
        vc = value_class_of(obj["_declared_value"])
        if vc:
            contention_penalty += vc["contention_penalty"]

    audit_rows = []
    total_setup = 0
    total_insurance = 0
    for obj in objects:
        lender = lenders.get(obj["lender_id"], {"num": 0, "den": 1})
        insurance = r_half_up(obj["declared_value_cents"] * lender["num"],
                              lender["den"])
        obj["_insurance"] = insurance
        total_setup += obj["_setup_seconds"]
        total_insurance += insurance
        audit_rows.append({
            "object_id": obj["object_id"],
            "placed_bay": obj["_bay"]["bay_id"] if obj["_bay"] else "",
            "crate_class": obj["_crate"] if obj["_crate"] else "",
            "displaced": 1 if obj["object_id"] in set(displaced_ids) else 0,
            "scheduled": obj["_scheduled"],
            "install_position": position[obj["object_id"]],
            "setup_seconds": obj["_setup_seconds"],
            "insurance_cents": insurance,
        })

    placed_count = sum(1 for o in objects if o["_scheduled"])
    total_residual = 0
    for obj in objects:
        if obj["_crate"]:
            for residual, crate_class, _ in obj["_crates"]:
                if crate_class == obj["_crate"]:
                    total_residual += residual
                    break

    objective = [placed_count, unsched_penalty, contention_penalty,
                 len(removed_edges), total_residual]

    # ---- emit artifacts
    write_jsonl(os.path.join(out_dir, "canonical_objects.jsonl"), [{
        "object_id": o["object_id"], "lender_id": o["lender_id"],
        "material_code": o["material_code"], "version": o["version"],
        "declared_value_cents": o["declared_value_cents"], "mass_g": o["mass_g"],
    } for o in objects])
    write_jsonl(os.path.join(out_dir, "normalized_geometry.jsonl"), [{
        "object_id": o["object_id"], "clearance_w": o["_cw"],
        "clearance_h": o["_ch"], "clearance_d": o["_cd"],
        "aspect_num": o["aspect_num"], "aspect_den": o["aspect_den"],
    } for o in objects])
    write_jsonl(os.path.join(out_dir, "admissibility_matrix.jsonl"), [{
        "object_id": o["object_id"], "orientations": o["orientations"],
        "tip_allowed": o["tip_allowed"], "rotate_allowed": o["rotate_allowed"],
    } for o in objects])
    write_jsonl(os.path.join(out_dir, "bay_candidate_matrix.jsonl"), bay_rows)
    write_jsonl(os.path.join(out_dir, "selected_placement.jsonl"), selected_rows)
    write_jsonl(os.path.join(out_dir, "rejected_placement.jsonl"),
                sorted(rejected_rows, key=lambda r: (r["object_id"], r["code"])))
    write_jsonl(os.path.join(out_dir, "conservation_adjusted.jsonl"), cons_rows)
    write_jsonl(os.path.join(out_dir, "crate_candidate_matrix.jsonl"),
                crate_cand_rows)
    write_jsonl(os.path.join(out_dir, "crate_assignment.jsonl"), crate_rows)
    write_jsonl(os.path.join(out_dir, "crate_capacity_ledger.jsonl"), ledger_rows)
    write_jsonl(os.path.join(out_dir, "crate_contention_report.jsonl"),
                contention_rows)
    write_jsonl(os.path.join(out_dir, "transport_leg_manifest.jsonl"),
                transport_rows)
    write_jsonl(os.path.join(out_dir, "dependency_edges.jsonl"), edge_rows)
    write_jsonl(os.path.join(out_dir, "cycle_resolution_audit.jsonl"),
                removed_edges)
    write_jsonl(os.path.join(out_dir, "install_order.jsonl"), install_rows)
    write_jsonl(os.path.join(out_dir, "crew_requirement_ledger.jsonl"), crew_rows)
    write_jsonl(os.path.join(out_dir, "install_window_table.jsonl"), window_rows)
    write_jsonl(os.path.join(out_dir, "unschedulable_report.jsonl"), unsched_rows)
    write_jsonl(os.path.join(out_dir, "gallery_occupancy_map.jsonl"),
                occupancy_rows)
    write_jsonl(os.path.join(out_dir, "deterministic_audit.jsonl"), audit_rows)

    write_json_obj(os.path.join(out_dir, "penalty_summary.json"), {
        "unschedulable_penalty": unsched_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": len(removed_edges),
        "total_insurance_cents": total_insurance,
    })
    write_json_obj(os.path.join(out_dir, "objective_tuple.json"), {
        "objective": objective,
        "order": list(objective_cfg.get("order", [])),
    })

    # ---- integrity manifest and final state
    program_path = os.path.abspath(__file__)
    manifest = {}
    for name in HASHED_NAMES:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest["plan.py"] = sha256_file(program_path)
    write_json_obj(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256(
            (chain + manifest[name]).encode("utf-8")).hexdigest()
    write_json_obj(os.path.join(out_dir, "final_state.json"), {
        "chain": chain,
        "repair_converged": final_cycles == 0,
        "cycle_edges_removed": len(removed_edges),
        "final_cycle_count": final_cycles,
        "displacement_count": len(displaced_ids),
        "unschedulable_count": len(unsched_rows),
        "placed_objects": placed_count,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })
    return {
        "placed": placed_count,
        "unschedulable": len(unsched_rows),
        "displaced": len(displaced_ids),
        "cycles": len(removed_edges),
        "objective": objective,
    }


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = argv[1]
    out_dir = argv[2]
    if not os.path.isdir(data_dir):
        sys.stderr.write("no such data directory: %s\n" % data_dir)
        return 2
    os.makedirs(out_dir, exist_ok=True)
    derive(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
