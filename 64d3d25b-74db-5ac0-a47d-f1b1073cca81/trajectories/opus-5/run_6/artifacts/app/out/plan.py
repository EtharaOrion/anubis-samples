#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is ever formed.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

LEN_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
SECONDS_PER_DAY = 3600

KIND_RULE = "D_RULE"
KIND_CONSERVATION = "D_CONSERVATION"
KIND_BAYSIZE = "D_BAYSIZE"
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


# ---------------------------------------------------------------- arithmetic

def round_half_up(num, den):
    """Round num/den to an integer, halves going to the larger value."""
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
    return -(-num // den)


def ratio(num, den):
    """Reduce num/den; a zero denominator yields the sentinel 0/1."""
    if den == 0:
        return 0, 1
    g = gcd(abs(num), abs(den))
    if g == 0:
        return 0, 1
    return num // g, den // g


# ------------------------------------------------------------------ loading

def read_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(data_dir, name):
    with open(os.path.join(data_dir, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------------ serialization

def write_lines(out_dir, name, rows):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_object(out_dir, name, value):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------------------------------- graph

def find_cycle(nodes, adj):
    """Return the first cycle found by a depth first scan in sorted order."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    parent = {}
    for start in sorted(nodes):
        if color[start] != WHITE:
            continue
        color[start] = GREY
        stack = [(start, iter(sorted(adj.get(start, ()))))]
        while stack:
            node, it = stack[-1]
            pushed = False
            for nxt in it:
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GREY
                    parent[nxt] = node
                    stack.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    pushed = True
                    break
                if color.get(nxt) == GREY:
                    cycle = [(node, nxt)]
                    cur = node
                    while cur != nxt:
                        prev = parent[cur]
                        cycle.append((prev, cur))
                        cur = prev
                    cycle.reverse()
                    return cycle
            if not pushed:
                color[node] = BLACK
                stack.pop()
    return None


def adjacency(edge_keys):
    adj = {}
    for src, dst in edge_keys:
        adj.setdefault(src, []).append(dst)
    return adj


# ----------------------------------------------------------------- pipeline

def build(data_dir, out_dir):
    cfg = read_json(data_dir, "horizon_config.json")
    objective_order = read_json(data_dir, "objective_order.json")
    clearance_factor = LEN_FACTOR[cfg["clearance_unit"]]
    setup_div = int(cfg["setup_div"])
    chain_seed = cfg["chain_seed"]
    venue_exclusive = cfg["venue_end_convention"] == "exclusive"
    conservation_exclusive = cfg["conservation_end_convention"] == "exclusive"

    # ---- stage 1: canonical identity and units -------------------------
    order = []
    master = {}
    for row in read_csv(data_dir, "object_master.csv"):
        oid = row["object_id"]
        if oid in master:
            continue
        master[oid] = row
        order.append(oid)

    version = {}
    for row in read_csv(data_dir, "object_version_map.csv"):
        oid = row["object_id"]
        v = int(row["version"])
        if oid not in version or v > version[oid]:
            version[oid] = v

    dims = {r["object_id"]: r for r in read_csv(data_dir, "object_dimensions.csv")}
    masses = {r["object_id"]: r for r in read_csv(data_dir, "object_mass.csv")}
    values = {r["object_id"]: r for r in read_csv(data_dir, "object_value.csv")}
    orients = {r["object_id"]: r for r in read_csv(data_dir, "object_orientation.csv")}

    materials = {r["material_code"]: r for r in read_csv(data_dir, "material_rules.csv")}
    handling = {r["handling_class"]: r
                for r in read_csv(data_dir, "handling_class_rules.csv")}
    conservation = {r["conservation_class"]: r
                    for r in read_csv(data_dir, "conservation_rules.csv")}
    crew_roster = {r["crew_class"]: r for r in read_csv(data_dir, "crew_roster.csv")}
    lenders = {r["lender_id"]: r for r in read_csv(data_dir, "lender_master.csv")}

    bays = read_csv(data_dir, "gallery_bays.csv")
    bay_by_id = {r["bay_id"]: r for r in bays}
    bay_exceptions = {}
    for row in read_csv(data_dir, "bay_exceptions.csv"):
        bay_exceptions.setdefault(row["bay_id"], set()).add(row["blocked_class"])
    venue = {r["bay_id"]: r for r in read_csv(data_dir, "venue_windows.csv")}

    crates = read_csv(data_dir, "crate_catalog.csv")
    crate_by_id = {r["crate_class"]: r for r in crates}
    reservations = {}
    for row in read_csv(data_dir, "crate_reservations.csv"):
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + int(row["reserved_count"])
    prior = {}
    for row in read_csv(data_dir, "prior_state.csv"):
        cls = row["crate_class"]
        prior[cls] = prior.get(cls, 0) + int(row["consumed_count"])

    transport = {r["lender_id"]: r for r in read_csv(data_dir, "transport_rules.csv")}
    value_classes = read_csv(data_dir, "value_class_rules.csv")

    obj = {}
    canonical_rows = []
    for oid in order:
        m = master[oid]
        d = dims[oid]
        factor = LEN_FACTOR[d["dim_unit"]]
        mass_row = masses[oid]
        mass_g = int(mass_row["mass"]) * MASS_FACTOR[mass_row["mass_unit"]]
        declared_value = int(values[oid]["declared_value"])
        material = materials[m["material_code"]]
        state = {
            "object_id": oid,
            "lender_id": m["lender_id"],
            "material_code": m["material_code"],
            "version": version[oid],
            "declared_value": declared_value,
            "declared_value_cents": declared_value * 100,
            "mass_g": mass_g,
            "w_um": int(d["width"]) * factor,
            "h_um": int(d["height"]) * factor,
            "d_um": int(d["depth"]) * factor,
            "conservation_class": material["conservation_class"],
            "max_lux": int(material["max_lux"]),
            "handling_class": material["handling_class"],
            "tip_allowed": int(orients[oid]["tip_allowed"]),
            "rotate_allowed": int(orients[oid]["rotate_allowed"]),
        }
        obj[oid] = state
        canonical_rows.append({
            "object_id": oid,
            "lender_id": state["lender_id"],
            "material_code": state["material_code"],
            "version": state["version"],
            "declared_value_cents": state["declared_value_cents"],
            "mass_g": state["mass_g"],
        })

    # ---- stage 2: normalized geometry ----------------------------------
    geometry_rows = []
    for oid in order:
        s = obj[oid]
        h = handling[s["handling_class"]]
        s["clearance_w"] = s["w_um"] + 2 * int(h["clearance_w"]) * clearance_factor
        s["clearance_d"] = s["d_um"] + 2 * int(h["clearance_d"]) * clearance_factor
        s["clearance_h"] = s["h_um"] + int(h["clearance_h"]) * clearance_factor
        num, den = ratio(s["h_um"], s["w_um"])
        s["aspect_num"], s["aspect_den"] = num, den
        geometry_rows.append({
            "object_id": oid,
            "clearance_w": s["clearance_w"],
            "clearance_h": s["clearance_h"],
            "clearance_d": s["clearance_d"],
            "aspect_num": num,
            "aspect_den": den,
        })

    # ---- stage 3: orientation admissibility ----------------------------
    admissibility_rows = []
    for oid in order:
        s = obj[oid]
        orientations = [0]
        if s["rotate_allowed"]:
            orientations.append(1)
        if s["tip_allowed"]:
            orientations.extend([2, 3])
        s["orientations"] = orientations
        admissibility_rows.append({
            "object_id": oid,
            "orientations": orientations,
            "tip_allowed": s["tip_allowed"],
            "rotate_allowed": s["rotate_allowed"],
        })

    def oriented(s, index):
        """Footprint width, footprint depth and vertical extent."""
        cw, cd, ch = s["clearance_w"], s["clearance_d"], s["clearance_h"]
        if index == 0:
            return cw, cd, ch
        if index == 1:
            return cd, cw, ch
        if index == 2:
            return cw, ch, cd
        return ch, cw, cd

    # ---- stage 4: bay candidates ---------------------------------------
    bay_interior = {
        r["bay_id"]: (int(r["interior_w"]) * clearance_factor,
                      int(r["interior_h"]) * clearance_factor,
                      int(r["interior_d"]) * clearance_factor)
        for r in bays
    }

    def fitting_orientation(s, bay_id):
        iw, ih, idp = bay_interior[bay_id]
        for index in s["orientations"]:
            fw, fd, fv = oriented(s, index)
            if fw <= iw and fd < idp and fv <= ih:
                return index
        return -1

    bays_by_id = sorted(bay_by_id)
    bays_by_scan = sorted(bay_by_id,
                          key=lambda b: (int(bay_by_id[b]["bay_priority"]), b))

    bay_candidate_rows = []
    for oid in order:
        s = obj[oid]
        fits_map = {}
        for bay_id in bays_by_id:
            index = fitting_orientation(s, bay_id)
            fits_map[bay_id] = index
            bay_candidate_rows.append({
                "object_id": oid,
                "bay_id": bay_id,
                "fits": index >= 0,
                "orientation": index,
            })
        s["bay_fits"] = fits_map

    # ---- stage 5: selected placement -----------------------------------
    selected_rows = []
    rejected_rows = []
    for oid in order:
        s = obj[oid]
        chosen, chosen_index = "", -1
        for bay_id in bays_by_scan:
            if s["bay_fits"][bay_id] >= 0:
                chosen, chosen_index = bay_id, s["bay_fits"][bay_id]
                break
        if chosen:
            code = "P_OK"
        elif s["orientations"] == [0]:
            code = "P_NO_ORIENTATION"
        else:
            code = "P_NO_FIT"
        s["selected_bay"] = chosen
        s["selected_orientation"] = chosen_index
        s["selected_code"] = code
        selected_rows.append({
            "object_id": oid,
            "bay_id": chosen,
            "orientation": chosen_index,
            "code": code,
        })
        if code != "P_OK":
            rejected_rows.append({"object_id": oid, "code": code})
    rejected_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 6: conservation adjustment ------------------------------
    def conservation_ok(s, bay_id):
        lux = int(bay_by_id[bay_id]["lux_level"])
        if lux > s["max_lux"]:
            return False
        if s["conservation_class"] in bay_exceptions.get(bay_id, ()):
            return False
        if int(conservation[s["conservation_class"]]["needs_dark"]) and lux > 0:
            return False
        return True

    conservation_rows = []
    occupancy = {bay_id: [] for bay_id in bays_by_id}
    for oid in order:
        s = obj[oid]
        chosen, chosen_index = "", -1
        for bay_id in bays_by_scan:
            if s["bay_fits"][bay_id] >= 0 and conservation_ok(s, bay_id):
                chosen, chosen_index = bay_id, s["bay_fits"][bay_id]
                break
        geometric = [b for b in bays_by_scan if s["bay_fits"][b] >= 0]
        if chosen:
            code = "P_OK"
        elif not geometric:
            code = s["selected_code"]
        elif len(geometric) == 1 and \
                s["conservation_class"] in bay_exceptions.get(geometric[0], ()):
            code = "P_BAY_EXCEPTION"
        else:
            code = "P_CONSERVATION_BLOCK"
        s["bay"] = chosen
        s["orientation"] = chosen_index
        s["placement_code"] = code
        conservation_rows.append({
            "object_id": oid,
            "bay_id": chosen,
            "orientation": chosen_index,
            "code": code,
        })
        if chosen:
            occupancy[chosen].append(oid)

    # ---- stage 7: crate candidates -------------------------------------
    crate_interior = {}
    crate_volume = {}
    for row in crates:
        iw = int(row["inner_w"]) * clearance_factor
        ih = int(row["inner_h"]) * clearance_factor
        idp = int(row["inner_d"]) * clearance_factor
        crate_interior[row["crate_class"]] = sorted((iw, ih, idp))
        crate_volume[row["crate_class"]] = iw * ih * idp

    crate_candidate_rows = []
    for oid in sorted(obj):
        s = obj[oid]
        margin = int(handling[s["handling_class"]]["handle_margin"]) * clearance_factor
        hw = s["w_um"] + margin
        hh = s["h_um"] + margin
        hd = s["d_um"] + margin
        s["handling_volume"] = hw * hh * hd
        want = sorted((hw, hh, hd))
        residuals = {}
        for crate_class in sorted(crate_by_id):
            inner = crate_interior[crate_class]
            if all(want[i] <= inner[i] for i in range(3)):
                residuals[crate_class] = crate_volume[crate_class] - s["handling_volume"]
        ranked = sorted(residuals, key=lambda c: (residuals[c], c))
        s["crate_residuals"] = residuals
        s["crate_ranked"] = ranked
        s["primary_crate"] = ranked[0] if ranked else ""
        for crate_class in ranked:
            crate_candidate_rows.append({
                "object_id": oid,
                "crate_class": crate_class,
                "residual": residuals[crate_class],
            })

    # ---- stage 13a: crew and setup seconds, needed by the install order -
    crew_rows = []
    for oid in order:
        s = obj[oid]
        h = handling[s["handling_class"]]
        s["crew_class"] = h["crew_class"]
        s["setup_seconds"] = round_half_up(int(h["setup_base"]) * s["mass_g"], setup_div)
        crew_rows.append({
            "object_id": oid,
            "crew_class": s["crew_class"],
            "setup_seconds": s["setup_seconds"],
        })

    # ---- stage 10: dependency edges ------------------------------------
    edges = {}

    def add_edge(src, dst, kind, weight):
        if src == dst:
            return
        current = edges.get((src, dst))
        if current is None or KIND_RANK[kind] > KIND_RANK[current[0]]:
            edges[(src, dst)] = (kind, weight)

    for bay_id in bays_by_id:
        members = sorted(occupancy[bay_id])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                va, vb = obj[a]["handling_volume"], obj[b]["handling_volume"]
                first, second = (a, b) if (va > vb or (va == vb and a < b)) else (b, a)
                add_edge(first, second, KIND_BAYSIZE, obj[first]["handling_volume"])
        for a in members:
            for b in members:
                if a != b and obj[a]["conservation_class"] > obj[b]["conservation_class"]:
                    add_edge(a, b, KIND_CONSERVATION, obj[a]["handling_volume"])

    for row in read_csv(data_dir, "dependency_rules.csv"):
        src, dst = row["from_object"], row["to_object"]
        if src in obj and dst in obj:
            add_edge(src, dst, KIND_RULE, int(row["edge_priority"]))

    dependency_rows = [
        {"from": src, "to": dst, "kind": kind, "weight": weight}
        for (src, dst), (kind, weight) in sorted(edges.items())
    ]

    # ---- stage 11: cycle resolution ------------------------------------
    live = dict(edges)
    audit_rows = []
    nodes = list(obj)
    for _ in range(len(edges) + 1):
        cycle = find_cycle(nodes, adjacency(live))
        if cycle is None:
            break
        worst = min(cycle,
                    key=lambda e: (KIND_RANK[live[e][0]], live[e][1], e[0], e[1]))
        kind, weight = live.pop(worst)
        audit_rows.append({"from": worst[0], "to": worst[1],
                           "kind": kind, "weight": weight})

    adj = adjacency(live)
    final_cycle_count = 0 if find_cycle(nodes, adj) is None else 1
    converged = final_cycle_count == 0

    # ---- stage 12: install order ---------------------------------------
    indegree = {oid: 0 for oid in obj}
    for _src, dst in live:
        indegree[dst] += 1
    ready = set(oid for oid in obj if indegree[oid] == 0)
    install_sequence = []
    while ready:
        pick = min(ready, key=lambda o: (obj[o]["setup_seconds"], o))
        ready.discard(pick)
        install_sequence.append(pick)
        for dst in sorted(adj.get(pick, ())):
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.add(dst)

    install_rows = []
    for position, oid in enumerate(install_sequence):
        obj[oid]["install_position"] = position
        install_rows.append({"position": position, "object_id": oid})

    # ---- stage 8: shared crate capacity --------------------------------
    remaining = {}
    ledger_base = {}
    for row in crates:
        cls = row["crate_class"]
        catalog = int(row["count"])
        consumed = prior.get(cls, 0)
        ledger_base[cls] = (catalog, consumed)
        remaining[cls] = catalog - consumed
    reserve_left = dict(reservations)

    for oid in install_sequence:
        s = obj[oid]
        available = [c for c in s["crate_ranked"] if remaining.get(c, 0) > 0]
        if not available:
            s["crate_class"] = ""
            s["crate_code"] = "C_NO_CRATE"
            continue
        lender = s["lender_id"]
        chosen = min(available, key=lambda c: (
            0 if reserve_left.get((c, lender), 0) > 0 else 1,
            s["crate_residuals"][c],
            c,
        ))
        remaining[chosen] -= 1
        if reserve_left.get((chosen, lender), 0) > 0:
            reserve_left[(chosen, lender)] -= 1
        s["crate_class"] = chosen
        s["crate_code"] = "C_PRIMARY" if chosen == s["primary_crate"] else "C_DISPLACED"

    crate_assignment_rows = []
    contention_rows = []
    for oid in sorted(obj):
        s = obj[oid]
        crate_assignment_rows.append({
            "object_id": oid,
            "crate_class": s["crate_class"],
            "code": s["crate_code"],
            "primary_class": s["primary_crate"],
        })
        if s["crate_code"] == "C_DISPLACED":
            contention_rows.append({
                "object_id": oid,
                "wanted_class": s["primary_crate"],
                "assigned_class": s["crate_class"],
            })

    capacity_rows = []
    for cls in sorted(crate_by_id):
        catalog, consumed = ledger_base[cls]
        capacity_rows.append({
            "crate_class": cls,
            "catalog_count": catalog,
            "prior_consumed": consumed,
            "remaining": remaining[cls],
        })

    # ---- stage 9: transport legs ---------------------------------------
    transport_rows = []
    for oid in order:
        s = obj[oid]
        if not s["crate_class"]:
            continue
        mode = crate_by_id[s["crate_class"]]["transport_mode"]
        exception = False
        rule = transport.get(s["lender_id"])
        if rule and rule["forbidden_mode"] == mode:
            mode = rule["fallback_mode"]
            exception = True
        transport_rows.append({
            "object_id": oid,
            "crate_class": s["crate_class"],
            "mode": mode,
            "exception": exception,
        })

    # ---- stage 13b: install windows ------------------------------------
    window_rows = []
    unschedulable_rows = []
    for oid in order:
        s = obj[oid]
        required_days = ceil_div(s["setup_seconds"], SECONDS_PER_DAY)
        bay_id = s["bay"]
        if not bay_id:
            low, high, code = 0, -1, "S_NO_PLACEMENT"
        else:
            cons = conservation[s["conservation_class"]]
            crew = crew_roster[s["crew_class"]]
            access = venue[bay_id]
            low = max(int(cons["setup_start"]), int(access["access_start"]),
                      int(crew["avail_start"]))
            high = min(int(cons["setup_end"]) - (1 if conservation_exclusive else 0),
                       int(access["access_end"]) - (1 if venue_exclusive else 0),
                       int(crew["avail_end"]))
            if not s["crate_class"]:
                code = "S_NO_CRATE"
            elif high < low:
                code = "S_NO_WINDOW"
            elif required_days > high - low + 1:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
        s["window_start"] = low
        s["window_end"] = high
        s["required_days"] = required_days
        s["schedule_code"] = code
        window_rows.append({
            "object_id": oid,
            "bay_id": bay_id,
            "window_start": low,
            "window_end": high,
            "required_days": required_days,
            "code": code,
        })
        if code != "S_OK":
            unschedulable_rows.append({"object_id": oid, "code": code})
    unschedulable_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 14: dossier ---------------------------------------------
    occupancy_rows = []
    for bay_id in bays_by_id:
        members = sorted(occupancy[bay_id])
        occupancy_rows.append({
            "bay_id": bay_id,
            "object_count": len(members),
            "objects": members,
        })

    ranked_value_classes = sorted(
        value_classes,
        key=lambda r: (int(r["min_value"]), r["value_class"]),
        reverse=True,
    )

    def value_class(declared):
        for row in ranked_value_classes:
            if declared >= int(row["min_value"]):
                return row
        return None

    unschedulable_penalty = 0
    contention_penalty = 0
    total_insurance = 0
    total_setup = 0
    total_residual = 0
    audit_out = []
    for oid in order:
        s = obj[oid]
        vc = value_class(s["declared_value"])
        if vc is not None:
            if s["schedule_code"] != "S_OK":
                unschedulable_penalty += int(vc["unschedulable_penalty"])
            if s["crate_code"] == "C_DISPLACED":
                contention_penalty += int(vc["contention_penalty"])
        lender = lenders[s["lender_id"]]
        s["insurance_cents"] = round_half_up(
            s["declared_value_cents"] * int(lender["insurance_rate_num"]),
            int(lender["insurance_rate_den"]),
        )
        total_insurance += s["insurance_cents"]
        total_setup += s["setup_seconds"]
        if s["crate_class"]:
            total_residual += s["crate_residuals"][s["crate_class"]]
        audit_out.append({
            "object_id": oid,
            "placed_bay": s["bay"],
            "crate_class": s["crate_class"],
            "displaced": s["crate_code"] == "C_DISPLACED",
            "scheduled": s["schedule_code"] == "S_OK",
            "install_position": s["install_position"],
            "setup_seconds": s["setup_seconds"],
            "insurance_cents": s["insurance_cents"],
        })

    placed_objects = sum(1 for s in obj.values() if s["schedule_code"] == "S_OK")
    displacement_count = sum(1 for s in obj.values() if s["crate_code"] == "C_DISPLACED")
    cycles_broken = len(audit_rows)
    objective = [placed_objects, unschedulable_penalty, contention_penalty,
                 cycles_broken, total_residual]

    # ---- emit -----------------------------------------------------------
    emit = {
        "canonical_objects.jsonl": canonical_rows,
        "normalized_geometry.jsonl": geometry_rows,
        "admissibility_matrix.jsonl": admissibility_rows,
        "bay_candidate_matrix.jsonl": bay_candidate_rows,
        "selected_placement.jsonl": selected_rows,
        "rejected_placement.jsonl": rejected_rows,
        "conservation_adjusted.jsonl": conservation_rows,
        "crate_candidate_matrix.jsonl": crate_candidate_rows,
        "crate_assignment.jsonl": crate_assignment_rows,
        "crate_capacity_ledger.jsonl": capacity_rows,
        "crate_contention_report.jsonl": contention_rows,
        "transport_leg_manifest.jsonl": transport_rows,
        "dependency_edges.jsonl": dependency_rows,
        "cycle_resolution_audit.jsonl": audit_rows,
        "install_order.jsonl": install_rows,
        "crew_requirement_ledger.jsonl": crew_rows,
        "install_window_table.jsonl": window_rows,
        "unschedulable_report.jsonl": unschedulable_rows,
        "gallery_occupancy_map.jsonl": occupancy_rows,
        "deterministic_audit.jsonl": audit_out,
    }
    for name in JSONL_ARTIFACTS:
        write_lines(out_dir, name, emit[name])

    write_object(out_dir, "penalty_summary.json", {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })
    write_object(out_dir, "objective_tuple.json", {
        "objective": objective,
        "order": list(objective_order["order"]),
    })

    program = os.path.abspath(__file__)
    manifest = {os.path.basename(program): sha256_file(program)}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    write_object(out_dir, "integrity_manifest.json", manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(out_dir, "final_state.json", {
        "chain": chain,
        "repair_converged": converged,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": final_cycle_count,
        "displacement_count": displacement_count,
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
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    build(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
