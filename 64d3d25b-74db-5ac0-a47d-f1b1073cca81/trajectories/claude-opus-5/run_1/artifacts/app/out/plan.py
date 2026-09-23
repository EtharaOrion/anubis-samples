#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is ever produced.
"""

import csv
import hashlib
import json
import os
import re
import sys
from math import gcd

LENGTH_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
MM = 1000
VALUE_TO_CENTS = 100
SECONDS_PER_DAY = 3600

KIND_RULE = "D_RULE"
KIND_CONSERVATION = "D_CONSERVATION"
KIND_BAYSIZE = "D_BAYSIZE"
# ascending removal precedence: the lowest rank is removed first
KIND_RANK = {KIND_BAYSIZE: 0, KIND_CONSERVATION: 1, KIND_RULE: 2}

JSONL_ARTIFACTS = [
    "canonical_objects.jsonl",
    "normalized_geometry.jsonl",
    "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl",
    "selected_placement.jsonl",
    "rejected_placement.jsonl",
    "conservation_adjusted.jsonl",
    "crate_candidate_matrix.jsonl",
    "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl",
    "crate_contention_report.jsonl",
    "transport_leg_manifest.jsonl",
    "dependency_edges.jsonl",
    "cycle_resolution_audit.jsonl",
    "install_order.jsonl",
    "crew_requirement_ledger.jsonl",
    "install_window_table.jsonl",
    "unschedulable_report.jsonl",
    "gallery_occupancy_map.jsonl",
    "deterministic_audit.jsonl",
]


# --------------------------------------------------------------------------
# exact arithmetic helpers
# --------------------------------------------------------------------------

def round_half_up(num, den):
    """Round num/den to an integer; an exact half goes to the larger quantum."""
    if den == 0:
        return 0
    if den < 0:
        num, den = -num, -den
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def ceil_div(num, den):
    if den == 0:
        return 0
    q, r = divmod(num, den)
    if r:
        q += 1
    return q


def reduce_ratio(num, den):
    """Reduce num/den; a zero denominator yields the sentinel 0/1."""
    if den == 0:
        return 0, 1
    g = gcd(abs(num), abs(den))
    if g == 0:
        return 0, 1
    return num // g, den // g


def natural_key(text):
    m = re.match(r"^(.*?)(\d+)$", text)
    if m:
        return (0, m.group(1), int(m.group(2)), "")
    return (1, "", 0, text)


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_csv(data_dir, name):
    with open(os.path.join(data_dir, name), newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def load_snapshot(data_dir):
    snap = {
        "horizon": read_json(data_dir, "horizon_config.json"),
        "objective_order": read_json(data_dir, "objective_order.json"),
    }
    for name in (
        "object_master", "object_dimensions", "object_mass", "object_value",
        "object_orientation", "object_version_map", "material_rules",
        "handling_class_rules", "gallery_bays", "bay_exceptions",
        "crate_catalog", "crate_reservations", "prior_state",
        "conservation_rules", "venue_windows", "dependency_rules",
        "crew_roster", "transport_rules", "lender_master", "value_class_rules",
    ):
        snap[name] = read_csv(data_dir, name + ".csv")
    return snap


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def dump_lines(out_dir, name, rows):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def dump_object(out_dir, name, obj):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2))
        fh.write("\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------
# graph helpers
# --------------------------------------------------------------------------

def find_cycle(nodes, adj):
    """First cycle found by DFS over ascending nodes and ascending successors."""
    color = {n: 0 for n in nodes}
    for start in nodes:
        if color[start] != 0:
            continue
        color[start] = 1
        path = [start]
        onpath = {start}
        stack = [(start, iter(adj[start]))]
        while stack:
            u, it = stack[-1]
            advanced = False
            for v in it:
                if v in onpath:
                    return path[path.index(v):] + [v]
                if color[v] == 0:
                    color[v] = 1
                    path.append(v)
                    onpath.add(v)
                    stack.append((v, iter(adj[v])))
                    advanced = True
                    break
            if not advanced:
                color[u] = 2
                stack.pop()
                path.pop()
                onpath.discard(u)
    return None


# --------------------------------------------------------------------------
# the derivation
# --------------------------------------------------------------------------

def build(data_dir, out_dir, program_path):
    snap = load_snapshot(data_dir)

    # ---- reference tables -------------------------------------------------
    material = {r["material_code"]: r for r in snap["material_rules"]}
    handling = {r["handling_class"]: r for r in snap["handling_class_rules"]}
    lender = {r["lender_id"]: r for r in snap["lender_master"]}
    conservation = {r["conservation_class"]: r for r in snap["conservation_rules"]}
    crew = {r["crew_class"]: r for r in snap["crew_roster"]}
    venue = {r["bay_id"]: r for r in snap["venue_windows"]}

    cons_classes = sorted({r["conservation_class"] for r in snap["conservation_rules"]},
                          key=natural_key)
    sensitivity = {c: i for i, c in enumerate(cons_classes)}

    bays = {}
    for r in snap["gallery_bays"]:
        bays[r["bay_id"]] = {
            "bay_id": r["bay_id"],
            "w": int(r["interior_w"]) * MM,
            "h": int(r["interior_h"]) * MM,
            "d": int(r["interior_d"]) * MM,
            "lux": int(r["lux_level"]),
            "priority": int(r["bay_priority"]),
        }
    bay_ids_sorted = sorted(bays)
    bay_scan_order = sorted(bays, key=lambda b: (bays[b]["priority"], b))

    blocked = {}
    for r in snap["bay_exceptions"]:
        blocked.setdefault(r["bay_id"], set()).add(r["blocked_class"])

    crates = {}
    for r in snap["crate_catalog"]:
        dims = sorted((int(r["inner_w"]) * MM, int(r["inner_h"]) * MM,
                       int(r["inner_d"]) * MM))
        crates[r["crate_class"]] = {
            "crate_class": r["crate_class"],
            "dims": dims,
            "volume": dims[0] * dims[1] * dims[2],
            "count": int(r["count"]),
            "mode": r["transport_mode"],
        }
    crate_ids_sorted = sorted(crates)

    prior_consumed = {c: 0 for c in crates}
    for r in snap["prior_state"]:
        prior_consumed[r["crate_class"]] = (
            prior_consumed.get(r["crate_class"], 0) + int(r["consumed_count"]))

    reservations = {}
    for r in snap["crate_reservations"]:
        key = (r["crate_class"], r["lender_id"])
        reservations[key] = reservations.get(key, 0) + int(r["reserved_count"])

    transport_rule = {}
    for r in snap["transport_rules"]:
        transport_rule.setdefault(r["lender_id"], []).append(r)

    value_classes = sorted(snap["value_class_rules"],
                           key=lambda r: (-int(r["min_value"]), r["value_class"]))

    dims_by_id = {r["object_id"]: r for r in snap["object_dimensions"]}
    mass_by_id = {r["object_id"]: r for r in snap["object_mass"]}
    value_by_id = {r["object_id"]: r for r in snap["object_value"]}
    orient_by_id = {r["object_id"]: r for r in snap["object_orientation"]}

    versions = {}
    for r in snap["object_version_map"]:
        oid, v = r["object_id"], int(r["version"])
        if oid not in versions or v > versions[oid]:
            versions[oid] = v

    setup_div = int(snap["horizon"]["setup_div"])

    # ---- stage 1: canonical identity and units ---------------------------
    order = []            # object ids in first-appearance order of the master
    obj = {}
    for r in snap["object_master"]:
        oid = r["object_id"]
        if oid in obj:
            continue          # stale duplicate row: the first occurrence wins
        order.append(oid)
        mat = material[r["material_code"]]
        dim = dims_by_id[oid]
        factor = LENGTH_FACTOR[dim["dim_unit"]]
        mass = mass_by_id[oid]
        orient = orient_by_id[oid]
        declared = int(value_by_id[oid]["declared_value"])
        obj[oid] = {
            "object_id": oid,
            "lender_id": r["lender_id"],
            "material_code": r["material_code"],
            "version": versions.get(oid, 0),
            "declared_value": declared,
            "declared_value_cents": declared * VALUE_TO_CENTS,
            "mass_g": int(mass["mass"]) * MASS_FACTOR[mass["mass_unit"]],
            "w": int(dim["width"]) * factor,
            "h": int(dim["height"]) * factor,
            "d": int(dim["depth"]) * factor,
            "tip": int(orient["tip_allowed"]),
            "rot": int(orient["rotate_allowed"]),
            "material": mat,
            "handling": handling[mat["handling_class"]],
            "cons_class": mat["conservation_class"],
            "max_lux": int(mat["max_lux"]),
        }
    ids_sorted = sorted(obj)

    canonical_rows = [{
        "object_id": obj[oid]["object_id"],
        "lender_id": obj[oid]["lender_id"],
        "material_code": obj[oid]["material_code"],
        "version": obj[oid]["version"],
        "declared_value_cents": obj[oid]["declared_value_cents"],
        "mass_g": obj[oid]["mass_g"],
    } for oid in order]

    # ---- stage 2: normalized geometry ------------------------------------
    geometry_rows = []
    for oid in order:
        o = obj[oid]
        hc = o["handling"]
        o["cw"] = o["w"] + 2 * int(hc["clearance_w"]) * MM
        o["cd"] = o["d"] + 2 * int(hc["clearance_d"]) * MM
        o["ch"] = o["h"] + int(hc["clearance_h"]) * MM
        num, den = reduce_ratio(o["h"], o["w"])
        geometry_rows.append({
            "object_id": oid,
            "clearance_w": o["cw"],
            "clearance_h": o["ch"],
            "clearance_d": o["cd"],
            "aspect_num": num,
            "aspect_den": den,
        })

    # ---- stage 3: orientation admissibility ------------------------------
    admissibility_rows = []
    for oid in order:
        o = obj[oid]
        orients = [0]
        if o["rot"]:
            orients.append(1)
        if o["tip"]:
            orients.extend((2, 3))
        o["orientations"] = orients
        admissibility_rows.append({
            "object_id": oid,
            "orientations": list(orients),
            "tip_allowed": o["tip"],
            "rotate_allowed": o["rot"],
        })

    def oriented(o, idx):
        """Return (footprint_w, footprint_d, vertical_extent) for an index."""
        cw, cd, ch = o["cw"], o["cd"], o["ch"]
        if idx == 0:
            return cw, cd, ch
        if idx == 1:
            return cd, cw, ch
        if idx == 2:
            return cw, ch, cd
        return ch, cw, cd

    def fit_orientation(o, bay):
        """Smallest admissible orientation index that fits the bay, else None."""
        for idx in o["orientations"]:
            fw, fd, fv = oriented(o, idx)
            if fw <= bay["w"] and fd < bay["d"] and fv <= bay["h"]:
                return idx
        return None

    # ---- stage 4: bay candidates -----------------------------------------
    candidate_rows = []
    for oid in order:
        o = obj[oid]
        o["geo_fit"] = {}
        for bid in bay_ids_sorted:
            idx = fit_orientation(o, bays[bid])
            if idx is not None:
                o["geo_fit"][bid] = idx
            candidate_rows.append({
                "object_id": oid,
                "bay_id": bid,
                "fits": idx is not None,
                "orientation": -1 if idx is None else idx,
            })

    # ---- stage 5: selected placement -------------------------------------
    selected_rows = []
    rejected_rows = []
    for oid in order:
        o = obj[oid]
        chosen = next((b for b in bay_scan_order if b in o["geo_fit"]), None)
        if chosen is None:
            code = "P_NO_ORIENTATION" if o["orientations"] == [0] else "P_NO_FIT"
            o["selected"] = (None, -1, code)
            rejected_rows.append({"object_id": oid, "code": code})
        else:
            o["selected"] = (chosen, o["geo_fit"][chosen], "P_OK")
        bid, idx, code = o["selected"]
        selected_rows.append({
            "object_id": oid,
            "bay_id": bid if bid is not None else "",
            "orientation": idx,
            "code": code,
        })
    rejected_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 6: conservation adjustment --------------------------------
    def conservation_ok(o, bay):
        if bay["lux"] > o["max_lux"]:
            return False
        if o["cons_class"] in blocked.get(bay["bay_id"], ()):
            return False
        if int(conservation[o["cons_class"]]["needs_dark"]) and bay["lux"] > 0:
            return False
        return True

    conservation_rows = []
    for oid in order:
        o = obj[oid]
        chosen = next((b for b in bay_scan_order
                       if b in o["geo_fit"] and conservation_ok(o, bays[b])), None)
        if chosen is not None:
            o["placed"] = (chosen, o["geo_fit"][chosen], "P_OK")
        elif not o["geo_fit"]:
            o["placed"] = (None, -1, o["selected"][2])
        else:
            fitting = list(o["geo_fit"])
            only = fitting[0] if len(fitting) == 1 else None
            if only is not None and o["cons_class"] in blocked.get(only, ()):
                o["placed"] = (None, -1, "P_BAY_EXCEPTION")
            else:
                o["placed"] = (None, -1, "P_CONSERVATION_BLOCK")
        bid, idx, code = o["placed"]
        conservation_rows.append({
            "object_id": oid,
            "bay_id": bid if bid is not None else "",
            "orientation": idx,
            "code": code,
        })

    # ---- stage 7: crate candidates ---------------------------------------
    crate_candidate_rows = []
    for oid in ids_sorted:
        o = obj[oid]
        margin = int(o["handling"]["handle_margin"]) * MM
        hdims = sorted((o["w"] + margin, o["h"] + margin, o["d"] + margin))
        o["handling_volume"] = hdims[0] * hdims[1] * hdims[2]
        fits = {}
        for cid in crate_ids_sorted:
            cdims = crates[cid]["dims"]
            if all(hdims[i] <= cdims[i] for i in range(3)):
                fits[cid] = crates[cid]["volume"] - o["handling_volume"]
        o["crate_fits"] = fits
        ranked = sorted(fits, key=lambda c: (fits[c], c))
        o["primary_class"] = ranked[0] if ranked else ""
        for cid in ranked:
            crate_candidate_rows.append({
                "object_id": oid,
                "crate_class": cid,
                "residual": fits[cid],
            })

    # ---- stage 13a: crew and setup seconds, needed by the install order ---
    for oid in order:
        o = obj[oid]
        o["crew_class"] = o["handling"]["crew_class"]
        o["setup_seconds"] = round_half_up(
            int(o["handling"]["setup_base"]) * o["mass_g"], setup_div)

    crew_rows = [{
        "object_id": oid,
        "crew_class": obj[oid]["crew_class"],
        "setup_seconds": obj[oid]["setup_seconds"],
    } for oid in order]

    # ---- stage 10: dependency edges --------------------------------------
    edges = {}

    def add_edge(frm, to, kind, weight):
        if frm == to:
            return
        cur = edges.get((frm, to))
        if cur is None or KIND_RANK[kind] > KIND_RANK[cur["kind"]]:
            edges[(frm, to)] = {"from": frm, "to": to, "kind": kind, "weight": weight}

    by_bay = {}
    for oid in ids_sorted:
        bid = obj[oid]["placed"][0]
        if bid is not None:
            by_bay.setdefault(bid, []).append(oid)

    for bid in sorted(by_bay):
        members = by_bay[bid]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                va, vb = obj[a]["handling_volume"], obj[b]["handling_volume"]
                first, second = (a, b) if (va > vb or (va == vb and a < b)) else (b, a)
                add_edge(first, second, KIND_BAYSIZE, obj[first]["handling_volume"])

    for bid in sorted(by_bay):
        members = by_bay[bid]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                sa = sensitivity[obj[a]["cons_class"]]
                sb = sensitivity[obj[b]["cons_class"]]
                if sa == sb:
                    continue
                first, second = (a, b) if sa > sb else (b, a)
                add_edge(first, second, KIND_CONSERVATION, obj[first]["handling_volume"])

    for r in snap["dependency_rules"]:
        if r["from_object"] in obj and r["to_object"] in obj:
            add_edge(r["from_object"], r["to_object"], KIND_RULE, int(r["edge_priority"]))

    dependency_rows = [dict(edges[k]) for k in sorted(edges)]

    # ---- stage 11: cycle resolution --------------------------------------
    live = dict(edges)
    removed = []
    converged = True
    guard = len(edges) + 1
    while True:
        adj = {n: [] for n in ids_sorted}
        for frm, to in live:
            adj[frm].append(to)
        for n in adj:
            adj[n].sort()
        cycle = find_cycle(ids_sorted, adj)
        if cycle is None:
            break
        if len(removed) >= guard:
            converged = False
            break
        cyc_edges = [live[(cycle[i], cycle[i + 1])] for i in range(len(cycle) - 1)]
        victim = min(cyc_edges, key=lambda e: (KIND_RANK[e["kind"]], e["weight"],
                                               e["from"], e["to"]))
        del live[(victim["from"], victim["to"])]
        removed.append(dict(victim))

    final_cycle_count = 0 if converged else 1

    # ---- stage 12: install order -----------------------------------------
    succ = {n: [] for n in ids_sorted}
    indeg = {n: 0 for n in ids_sorted}
    for frm, to in live:
        succ[frm].append(to)
        indeg[to] += 1
    ready = [n for n in ids_sorted if indeg[n] == 0]
    install_order = []
    while ready:
        ready.sort(key=lambda n: (obj[n]["setup_seconds"], n))
        pick = ready.pop(0)
        install_order.append(pick)
        for v in sorted(succ[pick]):
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
    if len(install_order) != len(ids_sorted):
        converged = False
        final_cycle_count = 1
        placed_set = set(install_order)
        install_order.extend(n for n in ids_sorted if n not in placed_set)

    install_position = {oid: i + 1 for i, oid in enumerate(install_order)}
    install_rows = [{"position": i + 1, "object_id": oid}
                    for i, oid in enumerate(install_order)]

    # ---- stage 8: shared crate capacity ----------------------------------
    remaining = {c: crates[c]["count"] - prior_consumed.get(c, 0) for c in crates}
    reserved_left = dict(reservations)
    assignment = {}
    displaced = set()
    for oid in install_order:
        o = obj[oid]
        options = [c for c in o["crate_fits"] if remaining.get(c, 0) > 0]
        if not options:
            assignment[oid] = ""
            continue
        pick = min(options, key=lambda c: (
            0 if reserved_left.get((c, o["lender_id"]), 0) > 0 else 1,
            o["crate_fits"][c], c))
        remaining[pick] -= 1
        key = (pick, o["lender_id"])
        if reserved_left.get(key, 0) > 0:
            reserved_left[key] -= 1
        assignment[oid] = pick
        if pick != o["primary_class"]:
            displaced.add(oid)

    crate_assignment_rows = []
    contention_rows = []
    for oid in ids_sorted:
        cid = assignment[oid]
        if not cid:
            code = "C_NO_CRATE"
        elif oid in displaced:
            code = "C_DISPLACED"
        else:
            code = "C_PRIMARY"
        crate_assignment_rows.append({
            "object_id": oid,
            "crate_class": cid,
            "code": code,
            "primary_class": obj[oid]["primary_class"],
        })
        if code == "C_DISPLACED":
            contention_rows.append({
                "object_id": oid,
                "wanted_class": obj[oid]["primary_class"],
                "assigned_class": cid,
            })

    ledger_rows = [{
        "crate_class": cid,
        "catalog_count": crates[cid]["count"],
        "prior_consumed": prior_consumed.get(cid, 0),
        "remaining": remaining[cid],
    } for cid in crate_ids_sorted]

    # ---- stage 9: transport legs -----------------------------------------
    transport_rows = []
    for oid in order:
        cid = assignment[oid]
        if not cid:
            continue
        mode = crates[cid]["mode"]
        exception = False
        for rule in transport_rule.get(obj[oid]["lender_id"], ()):
            if rule["forbidden_mode"] == mode:
                mode = rule["fallback_mode"]
                exception = True
                break
        transport_rows.append({
            "object_id": oid,
            "crate_class": cid,
            "mode": mode,
            "exception": exception,
        })

    # ---- stage 13b: install windows --------------------------------------
    window_rows = []
    unschedulable_rows = []
    for oid in order:
        o = obj[oid]
        bid = o["placed"][0]
        required_days = ceil_div(o["setup_seconds"], SECONDS_PER_DAY)
        if bid is None:
            low, high = 0, -1
            code = "S_NO_PLACEMENT"
        else:
            cwin = conservation[o["cons_class"]]
            vwin = venue[bid]
            cav = crew[o["crew_class"]]
            low = max(int(cwin["setup_start"]), int(vwin["access_start"]),
                      int(cav["avail_start"]))
            high = min(int(cwin["setup_end"]), int(vwin["access_end"]) - 1,
                       int(cav["avail_end"]))
            if not assignment[oid]:
                code = "S_NO_CRATE"
            elif high < low:
                code = "S_NO_WINDOW"
            elif required_days > high - low + 1:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
        o["code"] = code
        window_rows.append({
            "object_id": oid,
            "bay_id": bid if bid is not None else "",
            "window_start": low,
            "window_end": high,
            "required_days": required_days,
            "code": code,
        })
        if code != "S_OK":
            unschedulable_rows.append({"object_id": oid, "code": code})
    unschedulable_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 14: dossier -----------------------------------------------
    occupancy_rows = [{
        "bay_id": bid,
        "object_count": len(by_bay.get(bid, [])),
        "objects": sorted(by_bay.get(bid, [])),
    } for bid in bay_ids_sorted]

    def value_class(o):
        for r in value_classes:
            if o["declared_value"] >= int(r["min_value"]):
                return r
        return None

    unschedulable_penalty = 0
    contention_penalty = 0
    total_insurance = 0
    total_setup = 0
    total_residual = 0
    for oid in ids_sorted:
        o = obj[oid]
        vc = value_class(o)
        if o["code"] != "S_OK" and vc is not None:
            unschedulable_penalty += int(vc["unschedulable_penalty"])
        if oid in displaced and vc is not None:
            contention_penalty += int(vc["contention_penalty"])
        ldr = lender[o["lender_id"]]
        o["insurance_cents"] = round_half_up(
            o["declared_value_cents"] * int(ldr["insurance_rate_num"]),
            int(ldr["insurance_rate_den"]))
        total_insurance += o["insurance_cents"]
        total_setup += o["setup_seconds"]
        if assignment[oid]:
            total_residual += o["crate_fits"][assignment[oid]]

    placed_objects = sum(1 for oid in ids_sorted if obj[oid]["code"] == "S_OK")

    audit_rows = [{
        "object_id": oid,
        "placed_bay": obj[oid]["placed"][0] if obj[oid]["placed"][0] is not None else "",
        "crate_class": assignment[oid],
        "displaced": oid in displaced,
        "scheduled": obj[oid]["code"] == "S_OK",
        "install_position": install_position[oid],
        "setup_seconds": obj[oid]["setup_seconds"],
        "insurance_cents": obj[oid]["insurance_cents"],
    } for oid in order]

    cycles_broken = len(removed)
    objective = [placed_objects, unschedulable_penalty, contention_penalty,
                 cycles_broken, total_residual]

    # ---- emit -------------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)
    dump_lines(out_dir, "canonical_objects.jsonl", canonical_rows)
    dump_lines(out_dir, "normalized_geometry.jsonl", geometry_rows)
    dump_lines(out_dir, "admissibility_matrix.jsonl", admissibility_rows)
    dump_lines(out_dir, "bay_candidate_matrix.jsonl", candidate_rows)
    dump_lines(out_dir, "selected_placement.jsonl", selected_rows)
    dump_lines(out_dir, "rejected_placement.jsonl", rejected_rows)
    dump_lines(out_dir, "conservation_adjusted.jsonl", conservation_rows)
    dump_lines(out_dir, "crate_candidate_matrix.jsonl", crate_candidate_rows)
    dump_lines(out_dir, "crate_assignment.jsonl", crate_assignment_rows)
    dump_lines(out_dir, "crate_capacity_ledger.jsonl", ledger_rows)
    dump_lines(out_dir, "crate_contention_report.jsonl", contention_rows)
    dump_lines(out_dir, "transport_leg_manifest.jsonl", transport_rows)
    dump_lines(out_dir, "dependency_edges.jsonl", dependency_rows)
    dump_lines(out_dir, "cycle_resolution_audit.jsonl", removed)
    dump_lines(out_dir, "install_order.jsonl", install_rows)
    dump_lines(out_dir, "crew_requirement_ledger.jsonl", crew_rows)
    dump_lines(out_dir, "install_window_table.jsonl", window_rows)
    dump_lines(out_dir, "unschedulable_report.jsonl", unschedulable_rows)
    dump_lines(out_dir, "gallery_occupancy_map.jsonl", occupancy_rows)
    dump_lines(out_dir, "deterministic_audit.jsonl", audit_rows)

    dump_object(out_dir, "penalty_summary.json", {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })
    dump_object(out_dir, "objective_tuple.json", {
        "objective": objective,
        "order": list(snap["objective_order"]["order"]),
    })

    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    dump_object(out_dir, "integrity_manifest.json", manifest)

    chain = snap["horizon"]["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    dump_object(out_dir, "final_state.json", {
        "chain": chain,
        "repair_converged": converged,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": final_cycle_count,
        "displacement_count": len(displaced),
        "unschedulable_count": len(unschedulable_rows),
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    build(os.path.abspath(argv[1]), os.path.abspath(argv[2]),
          os.path.abspath(__file__))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
