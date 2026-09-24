#!/usr/bin/env python3
"""Installation dossier planner.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Derives the twenty four dossier artifacts from the snapshot in DATA_DIR and
writes them into OUT_DIR. All arithmetic is integer or exact rational
(fractions.Fraction); no floating point value is ever computed, compared or
written. Every stage that forms a rational quantity rounds it half up to that
stage's quantum and the rounded integer is what later stages consume.
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

SECONDS_PER_DAY = 3600
CENTS_PER_UNIT = 100

# Length unit codes -> micrometres (constants.md).
LEN_SCALE = {"UM": 1, "MM": 1000, "CM": 10000, "IN": 25400, "M": 1000000}
# Mass unit codes -> grams (constants.md).
MASS_SCALE = {"MG": Fraction(1, 1000), "G": 1, "KG": 1000, "T": 1000000}

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


# ---------------------------------------------------------------- helpers


def num(text):
    """Parse a numeric field exactly: int when integral, else a Fraction."""
    text = str(text).strip()
    try:
        return int(text)
    except ValueError:
        return Fraction(text)


def rhu(value):
    """Round a non negative exact rational half up to an integer."""
    frac = Fraction(value)
    den = frac.denominator
    quot = frac.numerator // den
    rem = frac.numerator - quot * den
    if 2 * rem >= den:
        quot += 1
    return quot


def quantum(value):
    """The integer a stage consumes: itself when integral, else rounded half up."""
    frac = Fraction(value)
    if frac.denominator == 1:
        return int(frac)
    return rhu(frac)


def reduced(num_, den_):
    """Reduced integer pair; a zero denominator yields the 0/1 sentinel."""
    if den_ == 0:
        return 0, 1
    frac = Fraction(num_, den_)
    return frac.numerator, frac.denominator


def read_rows(data_dir, name):
    """Read a csv as a list of dicts with stripped keys and values."""
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [c.strip() for c in next(reader)]
        except StopIteration:
            return []
        rows = []
        for raw in reader:
            if not raw or all(not c.strip() for c in raw):
                continue
            row = {}
            for i, key in enumerate(header):
                row[key] = raw[i].strip() if i < len(raw) else ""
            rows.append(row)
    return rows


def read_json(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def first_by_key(rows, key_fields):
    """Keep the first occurrence for each composite key, in file order."""
    seen = {}
    for row in rows:
        key = tuple(row[f] for f in key_fields)
        if len(key) == 1:
            key = key[0]
        if key not in seen:
            seen[key] = row
    return seen


def write_jsonl(out_dir, name, records):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_json(out_dir, name, value):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write("\n")


def file_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------------------- loading


def load_inputs(data_dir):
    horizon = read_json(data_dir, "horizon_config.json")
    objective = read_json(data_dir, "objective_order.json")

    master_rows = read_rows(data_dir, "object_master.csv")
    objects = []
    seen_ids = set()
    for row in master_rows:
        oid = row["object_id"]
        if oid in seen_ids:
            continue  # stale duplicate row: the first occurrence wins
        seen_ids.add(oid)
        objects.append(row)

    dims = first_by_key(read_rows(data_dir, "object_dimensions.csv"), ["object_id"])
    mass = first_by_key(read_rows(data_dir, "object_mass.csv"), ["object_id"])
    value = first_by_key(read_rows(data_dir, "object_value.csv"), ["object_id"])
    orient = first_by_key(read_rows(data_dir, "object_orientation.csv"), ["object_id"])

    versions = {}
    for row in read_rows(data_dir, "object_version_map.csv"):
        oid = row["object_id"]
        ver = int(row["version"])
        if oid not in versions or ver > versions[oid]:
            versions[oid] = ver  # highest version recorded for that id

    materials = first_by_key(read_rows(data_dir, "material_rules.csv"), ["material_code"])
    handling = first_by_key(read_rows(data_dir, "handling_class_rules.csv"), ["handling_class"])
    bays = read_rows(data_dir, "gallery_bays.csv")
    bay_exceptions = set()
    for row in read_rows(data_dir, "bay_exceptions.csv"):
        bay_exceptions.add((row["bay_id"], row["blocked_class"]))
    crates = read_rows(data_dir, "crate_catalog.csv")
    reservations = {}
    for row in read_rows(data_dir, "crate_reservations.csv"):
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + int(row["reserved_count"])
    prior = {}
    for row in read_rows(data_dir, "prior_state.csv"):
        prior[row["crate_class"]] = prior.get(row["crate_class"], 0) + int(row["consumed_count"])
    conservation = first_by_key(read_rows(data_dir, "conservation_rules.csv"), ["conservation_class"])
    venue = first_by_key(read_rows(data_dir, "venue_windows.csv"), ["bay_id"])
    dep_rules = read_rows(data_dir, "dependency_rules.csv")
    crew = first_by_key(read_rows(data_dir, "crew_roster.csv"), ["crew_class"])
    transport = first_by_key(read_rows(data_dir, "transport_rules.csv"), ["lender_id"])
    lenders = first_by_key(read_rows(data_dir, "lender_master.csv"), ["lender_id"])
    value_classes = read_rows(data_dir, "value_class_rules.csv")

    return {
        "horizon": horizon,
        "objective": objective,
        "objects": objects,
        "dims": dims,
        "mass": mass,
        "value": value,
        "orient": orient,
        "versions": versions,
        "materials": materials,
        "handling": handling,
        "bays": bays,
        "bay_exceptions": bay_exceptions,
        "crates": crates,
        "reservations": reservations,
        "prior": prior,
        "conservation": conservation,
        "venue": venue,
        "dep_rules": dep_rules,
        "crew": crew,
        "transport": transport,
        "lenders": lenders,
        "value_classes": value_classes,
    }


# ---------------------------------------------------------------- stages


def stage_canonical(data):
    """Stage 1: canonical identity, units and money."""
    horizon = data["horizon"]
    clearance_unit = horizon.get("clearance_unit", "MM")
    records = []
    for row in data["objects"]:
        oid = row["object_id"]
        dim = data["dims"].get(oid, {})
        mass = data["mass"].get(oid, {})
        val = data["value"].get(oid, {})
        len_factor = LEN_SCALE[dim.get("dim_unit", clearance_unit).strip().upper()]
        mass_factor = MASS_SCALE[mass.get("mass_unit", "G").strip().upper()]
        mass_g = quantum(num(mass.get("mass", 0)) * mass_factor)
        records.append(
            {
                "object_id": oid,
                "lender_id": row["lender_id"],
                "material_code": row["material_code"],
                "version": data["versions"].get(oid, 0),
                "declared_value_cents": int(num(val.get("declared_value", 0)) * CENTS_PER_UNIT),
                "mass_g": mass_g,
                "_width": quantum(num(dim.get("width", 0)) * len_factor),
                "_height": quantum(num(dim.get("height", 0)) * len_factor),
                "_depth": quantum(num(dim.get("depth", 0)) * len_factor),
            }
        )
    return records


def stage_geometry(data, canonical):
    """Stage 2: clearance envelope and aspect ratio."""
    margin_factor = LEN_SCALE[data["horizon"].get("clearance_unit", "MM").strip().upper()]
    out = []
    for rec in canonical:
        hrule = data["handling"][data["materials"][rec["material_code"]]["handling_class"]]
        cw_m = num(hrule["clearance_w"]) * margin_factor
        cd_m = num(hrule["clearance_d"]) * margin_factor
        ch_m = num(hrule["clearance_h"]) * margin_factor
        clearance_w = rec["_width"] + 2 * cw_m
        clearance_d = rec["_depth"] + 2 * cd_m
        clearance_h = rec["_height"] + ch_m
        aspect_num, aspect_den = reduced(rec["_height"], rec["_width"])
        out.append(
            {
                "object_id": rec["object_id"],
                "clearance_w": clearance_w,
                "clearance_h": clearance_h,
                "clearance_d": clearance_d,
                "aspect_num": aspect_num,
                "aspect_den": aspect_den,
            }
        )
        rec["_clearance"] = (clearance_w, clearance_h, clearance_d)
    return out


def orientation_footprint(index, clearance):
    """(footprint width, footprint depth, vertical extent) for an orientation."""
    cw, ch, cd = clearance
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def stage_admissibility(data, canonical):
    """Stage 3: the admissible orientation list."""
    for rec in canonical:
        row = data["orient"].get(rec["object_id"], {})
        tip = int(row.get("tip_allowed", 0) or 0)
        rot = int(row.get("rotate_allowed", 0) or 0)
        orientations = [0]
        if rot:
            orientations.append(1)
        if tip:
            orientations.extend([2, 3])
        rec["_tip"] = tip
        rec["_rotate"] = rot
        rec["_orientations"] = orientations
    return [
        {
            "object_id": rec["object_id"],
            "orientations": rec["_orientations"],
            "tip_allowed": rec["_tip"],
            "rotate_allowed": rec["_rotate"],
        }
        for rec in canonical
    ]


def bay_interiors(data):
    # constants.md: bay interior dimensions are declared in millimetres.
    factor = LEN_SCALE["MM"]
    interiors = {}
    for bay in data["bays"]:
        interiors[bay["bay_id"]] = (
            num(bay["interior_w"]) * factor,
            num(bay["interior_h"]) * factor,
            num(bay["interior_d"]) * factor,
        )
    return interiors


def stage_bay_candidates(data, canonical):
    """Stage 4: geometry only, width inclusive, depth exclusive, height inclusive."""
    interiors = bay_interiors(data)
    bay_ids = sorted(interiors)
    rows = []
    for rec in canonical:
        rec["_geo_fit"] = {}
        for bay_id in bay_ids:
            bw, bh, bd = interiors[bay_id]
            for index in rec["_orientations"]:
                fw, fd, fe = orientation_footprint(index, rec["_clearance"])
                if fw <= bw and fd < bd and fe <= bh:
                    rec["_geo_fit"][bay_id] = index
                    break
        for bay_id in bay_ids:
            rows.append(
                {
                    "object_id": rec["object_id"],
                    "bay_id": bay_id,
                    "fits": bay_id in rec["_geo_fit"],
                    "orientation": rec["_geo_fit"].get(bay_id, -1),
                }
            )
    return rows


def placement_scan(rec, bays_ordered, predicate):
    """First bay in priority then id order whose smallest fitting index exists."""
    for bay in bays_ordered:
        bay_id = bay["bay_id"]
        if bay_id in rec["_geo_fit"] and predicate(rec, bay):
            return bay_id, rec["_geo_fit"][bay_id]
    return None, None


def stage_selected_placement(data, canonical):
    """Stage 5: primary placement on geometry alone."""
    bays_ordered = sorted(data["bays"], key=lambda b: (int(num(b["bay_priority"])), b["bay_id"]))
    selected, rejected = [], []
    for rec in canonical:
        bay_id, index = placement_scan(rec, bays_ordered, lambda r, b: True)
        if bay_id is None:
            code = "P_NO_ORIENTATION" if rec["_orientations"] == [0] else "P_NO_FIT"
            rejected.append({"object_id": rec["object_id"], "code": code})
            rec["_stage5"] = (None, None, code)
        else:
            selected.append(
                {
                    "object_id": rec["object_id"],
                    "bay_id": bay_id,
                    "orientation": index,
                    "code": "P_OK",
                }
            )
            rec["_stage5"] = (bay_id, index, "P_OK")
    return selected, rejected


def conservation_ok(data, rec, bay):
    material = data["materials"][rec["material_code"]]
    lux = int(num(bay["lux_level"]))
    if lux > int(num(material["max_lux"])):
        return False
    if (bay["bay_id"], material["conservation_class"]) in data["bay_exceptions"]:
        return False
    con = data["conservation"].get(material["conservation_class"], {})
    if int(con.get("needs_dark", 0) or 0) and lux > 0:
        return False
    return True


def conservation_reason(data, rec, bays_ordered):
    """Why conservation found no bay for an object that fits somewhere."""
    fitting = [b for b in bays_ordered if b["bay_id"] in rec["_geo_fit"]]
    if not fitting:
        return rec["_stage5"][2]
    if len(fitting) == 1:
        material = data["materials"][rec["material_code"]]
        if (fitting[0]["bay_id"], material["conservation_class"]) in data["bay_exceptions"]:
            return "P_BAY_EXCEPTION"
    return "P_CONSERVATION_BLOCK"


def stage_conservation(data, canonical):
    """Stage 6: re run the scan honouring conservation."""
    bays_ordered = sorted(data["bays"], key=lambda b: (int(num(b["bay_priority"])), b["bay_id"]))
    rows = []
    for rec in canonical:
        bay_id, index = placement_scan(rec, bays_ordered, lambda r, b: conservation_ok(data, r, b))
        if bay_id is None:
            code = conservation_reason(data, rec, bays_ordered)
            rows.append(
                {"object_id": rec["object_id"], "bay_id": "", "orientation": -1, "code": code}
            )
            rec["_bay"] = None
            rec["_orientation"] = -1
            rec["_place_code"] = code
        else:
            rows.append(
                {
                    "object_id": rec["object_id"],
                    "bay_id": bay_id,
                    "orientation": index,
                    "code": "P_OK",
                }
            )
            rec["_bay"] = bay_id
            rec["_orientation"] = index
            rec["_place_code"] = "P_OK"
    return rows


def stage_crate_candidates(data, canonical):
    """Stage 7: handling envelope, fitting crates and residuals."""
    # constants.md: crate interior dimensions are declared in millimetres.
    factor = LEN_SCALE["MM"]
    rows = []
    for rec in canonical:
        hrule = data["handling"][data["materials"][rec["material_code"]]["handling_class"]]
        margin = num(hrule["handle_margin"]) * factor
        handle = tuple(
            rec[key] + margin for key in ("_width", "_height", "_depth")
        )
        handle_sorted = sorted(handle)
        handle_volume = handle[0] * handle[1] * handle[2]
        rec["_handle"] = handle
        rec["_handle_volume"] = handle_volume
        fitting = []
        for crate in data["crates"]:
            inner = tuple(
                num(crate[key]) * factor for key in ("inner_w", "inner_h", "inner_d")
            )
            if all(a <= b for a, b in zip(handle_sorted, sorted(inner))):
                volume = inner[0] * inner[1] * inner[2]
                fitting.append(
                    {"crate_class": crate["crate_class"], "residual": volume - handle_volume}
                )
        fitting.sort(key=lambda c: (c["residual"], c["crate_class"]))
        rec["_crates"] = fitting
        rec["_primary_crate"] = fitting[0]["crate_class"] if fitting else ""
        for cand in fitting:
            rows.append(
                {
                    "object_id": rec["object_id"],
                    "crate_class": cand["crate_class"],
                    "residual": cand["residual"],
                }
            )
    return rows


def stage_crew(data, canonical):
    """Stage 13 quantities that do not depend on crates: crew class and setup."""
    setup_div = int(num(data["horizon"].get("setup_div", 700)))
    for rec in canonical:
        material = data["materials"][rec["material_code"]]
        hrule = data["handling"][material["handling_class"]]
        crew_class = hrule["crew_class"]
        setup_base = num(hrule["setup_base"])
        setup_seconds = rhu(Fraction(setup_base * rec["mass_g"], setup_div))
        rec["_crew_class"] = crew_class
        rec["_setup_seconds"] = setup_seconds
    return [
        {
            "object_id": rec["object_id"],
            "crew_class": rec["_crew_class"],
            "setup_seconds": rec["_setup_seconds"],
        }
        for rec in canonical
    ]


KIND_RANK = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}
KIND_PRECEDENCE = {"D_RULE": 2, "D_CONSERVATION": 1, "D_BAYSIZE": 0}


def stage_dependency_edges(data, canonical):
    """Stage 10: explicit, conservation and bay size edges with dedupe."""
    by_id = {rec["object_id"]: rec for rec in canonical}
    edges = {}
    for row in data["dep_rules"]:
        key = (row["from_object"], row["to_object"])
        cand = (row["from_object"], row["to_object"], "D_RULE", int(num(row["edge_priority"])))
        if key not in edges or KIND_PRECEDENCE[cand[2]] > KIND_PRECEDENCE[edges[key][2]]:
            edges[key] = cand

    by_bay = {}
    for rec in canonical:
        if rec["_bay"] is not None:
            by_bay.setdefault(rec["_bay"], []).append(rec)

    for bay_id, members in by_bay.items():
        members = sorted(members, key=lambda r: r["object_id"])
        for i, first in enumerate(members):
            for second in members[i + 1:]:
                cls_a = data["materials"][first["material_code"]]["conservation_class"]
                cls_b = data["materials"][second["material_code"]]["conservation_class"]
                sens_a = int(cls_a[1:]) if cls_a[1:].isdigit() else 0
                sens_b = int(cls_b[1:]) if cls_b[1:].isdigit() else 0
                if sens_a != sens_b:
                    early, late = (
                        (first, second) if sens_a > sens_b else (second, first)
                    )
                    key = (early["object_id"], late["object_id"])
                    cand = (
                        early["object_id"],
                        late["object_id"],
                        "D_CONSERVATION",
                        early["_handle_volume"],
                    )
                    if key not in edges or KIND_PRECEDENCE[cand[2]] > KIND_PRECEDENCE[edges[key][2]]:
                        edges[key] = cand
                if first["_handle_volume"] != second["_handle_volume"]:
                    early, late = (
                        (first, second)
                        if first["_handle_volume"] > second["_handle_volume"]
                        else (second, first)
                    )
                else:
                    early, late = (
                        (first, second)
                        if first["object_id"] < second["object_id"]
                        else (second, first)
                    )
                key = (early["object_id"], late["object_id"])
                cand = (
                    early["object_id"],
                    late["object_id"],
                    "D_BAYSIZE",
                    early["_handle_volume"],
                )
                if key not in edges or KIND_PRECEDENCE[cand[2]] > KIND_PRECEDENCE[edges[key][2]]:
                    edges[key] = cand

    edge_list = []
    for key in sorted(edges):
        frm, to, kind, weight = edges[key]
        edge_list.append({"from": frm, "to": to, "kind": kind, "weight": weight})
    known = set(by_id)
    edge_list = [e for e in edge_list if e["from"] in known and e["to"] in known]
    return edge_list


def strongly_connected(nodes, edge_list):
    """Iterative Tarjan; returns a component index per node and component sizes."""
    adjacency = {node: [] for node in nodes}
    for edge in edge_list:
        if edge["from"] in adjacency and edge["to"] in adjacency:
            adjacency[edge["from"]].append(edge["to"])
    index = {}
    lowlink = {}
    on_stack = set()
    stack = []
    comp_of = {}
    comps = []
    counter = [0]

    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = counter[0]
                lowlink[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            advanced = False
            for i in range(pi, len(adjacency[node])):
                nxt = adjacency[node][i]
                if nxt not in index:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    advanced = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if lowlink[node] == index[node]:
                comp = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    comp_of[top] = len(comps)
                    comp.append(top)
                    if top == node:
                        break
                comps.append(comp)
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return comp_of, comps


def cycle_count(edge_list, nodes):
    """Number of cyclic strongly connected components in the graph."""
    comp_of, comps = strongly_connected(nodes, edge_list)
    cyclic = 0
    for i, comp in enumerate(comps):
        if len(comp) > 1:
            cyclic += 1
    self_looped = {e["from"] for e in edge_list if e["from"] == e["to"]}
    for i, comp in enumerate(comps):
        if len(comp) == 1 and comp[0] in self_looped:
            cyclic += 1
    return cyclic


def resolve_cycles(nodes, edge_list):
    """Stage 11: remove the lowest priority edge on a cycle until acyclic."""
    edges = [dict(e) for e in edge_list]
    removed = []
    while True:
        comp_of, comps = strongly_connected(nodes, edges)
        cyclic_comps = {i for i, comp in enumerate(comps) if len(comp) > 1}
        for edge in edges:  # a self loop is a cycle of length one
            if edge["from"] == edge["to"]:
                cyclic_comps.add(comp_of[edge["from"]])
        if not cyclic_comps:
            break
        candidates = [
            edge
            for edge in edges
            if edge["from"] == edge["to"]
            or (
                comp_of[edge["from"]] == comp_of[edge["to"]]
                and comp_of[edge["from"]] in cyclic_comps
            )
        ]
        if not candidates:
            break
        worst = min(
            candidates, key=lambda e: (KIND_RANK[e["kind"]], e["weight"], e["from"], e["to"])
        )
        edges.remove(worst)
        removed.append(worst)
    return edges, removed


def count_cycles(nodes, edges):
    """Number of cyclic strongly connected components after repair."""
    comp_of, comps = strongly_connected(nodes, edges)
    total = 0
    for i, comp in enumerate(comps):
        if len(comp) > 1:
            total += 1
    self_looped = {e["from"] for e in edges if e["from"] == e["to"]}
    for i, comp in enumerate(comps):
        if len(comp) == 1 and comp[0] in self_looped:
            total += 1
    return total


def stage_install_order(canonical, edges):
    """Stage 12: repeated selection, smallest setup seconds then object id."""
    by_id = {rec["object_id"]: rec for rec in canonical}
    nodes = [rec["object_id"] for rec in canonical]
    indegree = {node: 0 for node in nodes}
    successors = {node: [] for node in nodes}
    for edge in edges:
        if edge["from"] in successors and edge["to"] in indegree:
            successors[edge["from"]].append(edge["to"])
            indegree[edge["to"]] += 1
    for node in successors:
        successors[node].sort()
    ready = sorted(
        node for node in nodes if indegree[node] == 0
    )
    order = []
    remaining = dict(indegree)
    while ready:
        ready.sort(key=lambda n: (by_id[n]["_setup_seconds"], n))
        node = ready.pop(0)
        order.append(node)
        for nxt in successors[node]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                ready.append(nxt)
    for node in nodes:  # any leftover (should not happen once acyclic)
        if node not in order:
            order.append(node)
    return [{"position": i + 1, "object_id": oid} for i, oid in enumerate(order)]


def stage_crate_assignment(data, canonical, install_order):
    """Stage 8: shared crate capacity consumed in installation order."""
    by_id = {rec["object_id"]: rec for rec in canonical}
    remaining = {}
    for crate in data["crates"]:
        cls = crate["crate_class"]
        remaining[cls] = int(num(crate["count"])) - data["prior"].get(cls, 0)
    reservations = dict(data["reservations"])

    assignments = {}
    for rec in canonical:
        rec["_crate"] = ""
        rec["_crate_code"] = "C_NO_CRATE"
    for oid in [row["object_id"] for row in install_order]:
        rec = by_id[oid]
        available = [c for c in rec["_crates"] if remaining[c["crate_class"]] > 0]
        if not available:
            assignments[oid] = {
                "object_id": oid,
                "crate_class": "",
                "code": "C_NO_CRATE",
                "primary_class": rec["_primary_crate"],
            }
            rec["_crate"] = ""
            rec["_crate_code"] = "C_NO_CRATE"
            continue
        lender = rec["lender_id"]

        def sort_key(cand):
            reserved = 1 if reservations.get((cand["crate_class"], lender), 0) > 0 else 0
            return (0 if reserved else 1, cand["residual"], cand["crate_class"])

        chosen = min(available, key=sort_key)
        cls = chosen["crate_class"]
        remaining[cls] -= 1
        if reservations.get((cls, lender), 0) > 0:
            reservations[(cls, lender)] -= 1
        code = "C_PRIMARY" if cls == rec["_primary_crate"] else "C_DISPLACED"
        assignments[oid] = {
            "object_id": oid,
            "crate_class": cls,
            "code": code,
            "primary_class": rec["_primary_crate"],
        }
        rec["_crate"] = cls
        rec["_crate_code"] = code

    rows = [assignments[rec["object_id"]] for rec in canonical if rec["object_id"] in assignments]
    rows.sort(key=lambda r: r["object_id"])
    return rows, remaining


def stage_transport(data, canonical):
    """Stage 9: mode of the assigned crate class with lender fallback."""
    modes = {c["crate_class"]: c["transport_mode"] for c in data["crates"]}
    rows = []
    for rec in canonical:
        if not rec["_crate"]:
            continue
        mode = modes[rec["_crate"]]
        rule = data["transport"].get(rec["lender_id"])
        exception = bool(rule and rule["forbidden_mode"] == mode)
        if exception:
            mode = rule["fallback_mode"]
        rec["_mode"] = mode
        rows.append(
            {
                "object_id": rec["object_id"],
                "crate_class": rec["_crate"],
                "mode": mode,
                "exception": exception,
            }
        )
    return rows


def value_class_for(data, declared_value_cents):
    """Highest value class whose minimum value the object meets."""
    best = None
    for row in data["value_classes"]:
        if declared_value_cents >= int(num(row["min_value"])) * CENTS_PER_UNIT:
            key = int(num(row["min_value"]))
            if best is None or key > best[0]:
                best = (key, row)
    return best[1] if best else None


def stage_windows(data, canonical):
    """Stage 13: install windows, days required and schedule codes."""
    horizon = data["horizon"]
    venue_end_inclusive = horizon.get("venue_end_convention", "exclusive") == "inclusive"
    rows = []
    for rec in canonical:
        material = data["materials"][rec["material_code"]]
        con = data["conservation"].get(material["conservation_class"], {})
        crew = data["crew"].get(rec["_crew_class"], {})
        if rec["_bay"] is not None:
            venue = data["venue"].get(rec["_bay"], {})
            starts = [int(num(con.get("setup_start", 0)))]
            ends = [int(num(con.get("setup_end", 0)))]
            if venue:
                starts.append(int(num(venue["access_start"])))
                offset = 0 if venue_end_inclusive else 1
                ends.append(int(num(venue["access_end"])) - offset)
            if crew:
                starts.append(int(num(crew["avail_start"])))
                ends.append(int(num(crew["avail_end"])))
            low = max(starts)
            high = min(ends)
        else:
            low, high = 0, -1
        days_available = high - low + 1
        required_days = -(-rec["_setup_seconds"] // SECONDS_PER_DAY) if rec["_setup_seconds"] else 0
        if rec["_bay"] is None:
            code = "S_NO_PLACEMENT"
        elif not rec["_crate"]:
            code = "S_NO_CRATE"
        elif days_available < 1:
            code = "S_NO_WINDOW"
        elif required_days > days_available:
            code = "S_SHORT_WINDOW"
        else:
            code = "S_OK"
        rec["_scheduled"] = code == "S_OK"
        rec["_window"] = (low, high, required_days, code)
        bay_id = rec["_bay"] if rec["_bay"] is not None else ""
        rows.append(
            {
                "object_id": rec["object_id"],
                "bay_id": bay_id,
                "window_start": low,
                "window_end": high,
                "required_days": required_days,
                "code": code,
            }
        )
    return rows


def insurance_cents(data, rec):
    lender = data["lenders"].get(rec["lender_id"], {})
    den = int(num(lender.get("insurance_rate_den", 0)))
    numer = int(num(lender.get("insurance_rate_num", 0)))
    if den == 0:
        return 0  # zero denominator yields the 0/1 sentinel
    return rhu(Fraction(rec["declared_value_cents"] * numer, den))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    data = load_inputs(data_dir)
    canonical = stage_canonical(data)
    geometry = stage_geometry(data, canonical)
    admissibility = stage_admissibility(data, canonical)
    bay_candidates = stage_bay_candidates(data, canonical)
    selected, rejected = stage_selected_placement(data, canonical)
    conservation_rows = stage_conservation(data, canonical)
    crate_candidates = stage_crate_candidates(data, canonical)
    crew_rows = stage_crew(data, canonical)
    edge_list = stage_dependency_edges(data, canonical)
    nodes = [rec["object_id"] for rec in canonical]
    kept_edges, removed_edges = resolve_cycles(nodes, edge_list)
    install_order = stage_install_order(canonical, kept_edges)
    assignment_rows, crate_remaining = stage_crate_assignment(data, canonical, install_order)
    transport_rows = stage_transport(data, canonical)
    window_rows = stage_windows(data, canonical)

    position_of = {row["object_id"]: row["position"] for row in install_order}

    by_bay = {bay["bay_id"]: [] for bay in data["bays"]}
    for rec in canonical:
        if rec["_bay"] is not None:
            by_bay[rec["_bay"]].append(rec["object_id"])
    occupancy = [
        {
            "bay_id": bay_id,
            "object_count": len(sorted(members)),
            "objects": sorted(members),
        }
        for bay_id, members in sorted(by_bay.items())
    ]

    unschedulable = [
        {"object_id": rec["object_id"], "code": rec["_window"][3]}
        for rec in canonical
        if not rec["_scheduled"]
    ]
    unschedulable.sort(key=lambda r: (r["object_id"], r["code"]))

    ledger = []
    for crate in sorted(data["crates"], key=lambda c: c["crate_class"]):
        cls = crate["crate_class"]
        ledger.append(
            {
                "crate_class": cls,
                "catalog_count": int(num(crate["count"])),
                "prior_consumed": data["prior"].get(cls, 0),
                "remaining": crate_remaining[cls],
            }
        )

    contention = [
        {
            "object_id": rec["object_id"],
            "wanted_class": rec["_primary_crate"],
            "assigned_class": rec["_crate"],
        }
        for rec in canonical
        if rec["_crate_code"] == "C_DISPLACED"
    ]
    contention.sort(key=lambda r: r["object_id"])

    crate_resid = {
        (c["object_id"], c["crate_class"]): c["residual"] for c in crate_candidates
    }
    audit = []
    total_setup = 0
    total_insurance = 0
    total_residual = 0
    unschedulable_penalty = 0
    contention_penalty = 0
    for rec in canonical:
        ins = insurance_cents(data, rec)
        vclass = value_class_for(data, rec["declared_value_cents"])
        total_setup += rec["_setup_seconds"]
        total_insurance += ins
        if rec["_crate"]:
            total_residual += crate_resid[(rec["object_id"], rec["_crate"])]
        if not rec["_scheduled"] and vclass is not None:
            unschedulable_penalty += int(num(vclass["unschedulable_penalty"]))
        if rec["_crate_code"] == "C_DISPLACED" and vclass is not None:
            contention_penalty += int(num(vclass["contention_penalty"]))
        audit.append(
            {
                "object_id": rec["object_id"],
                "placed_bay": rec["_bay"] if rec["_bay"] is not None else "",
                "crate_class": rec["_crate"],
                "displaced": rec["_crate_code"] == "C_DISPLACED",
                "scheduled": rec["_scheduled"],
                "install_position": position_of.get(rec["object_id"], 0),
                "setup_seconds": rec["_setup_seconds"],
                "insurance_cents": ins,
            }
        )

    placed_objects = sum(1 for rec in canonical if rec["_scheduled"])

    write_jsonl(out_dir, "canonical_objects.jsonl", [
        {
            "object_id": r["object_id"],
            "lender_id": r["lender_id"],
            "material_code": r["material_code"],
            "version": r["version"],
            "declared_value_cents": r["declared_value_cents"],
            "mass_g": r["mass_g"],
        }
        for r in canonical
    ])
    write_jsonl(out_dir, "normalized_geometry.jsonl", geometry)
    write_jsonl(out_dir, "admissibility_matrix.jsonl", admissibility)
    write_jsonl(out_dir, "bay_candidate_matrix.jsonl", bay_candidates)
    write_jsonl(out_dir, "selected_placement.jsonl", selected)
    write_jsonl(out_dir, "rejected_placement.jsonl", sorted(
        rejected, key=lambda r: (r["object_id"], r["code"])))
    write_jsonl(out_dir, "conservation_adjusted.jsonl", conservation_rows)
    write_jsonl(out_dir, "crate_candidate_matrix.jsonl", sorted(
        crate_candidates, key=lambda r: (r["object_id"], r["residual"], r["crate_class"])))
    write_jsonl(out_dir, "crate_assignment.jsonl", assignment_rows)
    write_jsonl(out_dir, "crate_capacity_ledger.jsonl", ledger)
    write_jsonl(out_dir, "crate_contention_report.jsonl", contention)
    write_jsonl(out_dir, "transport_leg_manifest.jsonl", transport_rows)
    write_jsonl(out_dir, "dependency_edges.jsonl", sorted(
        edge_list, key=lambda e: (e["from"], e["to"])))
    write_jsonl(out_dir, "cycle_resolution_audit.jsonl", removed_edges)
    write_jsonl(out_dir, "install_order.jsonl", install_order)
    write_jsonl(out_dir, "crew_requirement_ledger.jsonl", crew_rows)
    write_jsonl(out_dir, "install_window_table.jsonl", window_rows)
    write_jsonl(out_dir, "unschedulable_report.jsonl", unschedulable)
    write_jsonl(out_dir, "gallery_occupancy_map.jsonl", occupancy)
    write_jsonl(out_dir, "deterministic_audit.jsonl", audit)

    penalty_summary = {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": len(removed_edges),
        "total_insurance_cents": total_insurance,
    }
    write_json(out_dir, "penalty_summary.json", penalty_summary)

    objective_tuple = {
        "objective": [
            placed_objects,
            unschedulable_penalty,
            contention_penalty,
            len(removed_edges),
            total_residual,
        ],
        "order": data["objective"]["order"],
    }
    write_json(out_dir, "objective_tuple.json", objective_tuple)

    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = file_digest(os.path.join(out_dir, name))
    program_path = os.path.abspath(__file__)
    manifest[os.path.basename(program_path)] = file_digest(program_path)
    write_json(out_dir, "integrity_manifest.json", manifest)

    chain = data["horizon"].get(
        "chain_seed", "0" * 64
    )
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    final_state = {
        "chain": chain,
        "repair_converged": count_cycles(nodes, kept_edges) == 0,
        "cycle_edges_removed": len(removed_edges),
        "final_cycle_count": count_cycles(nodes, kept_edges),
        "displacement_count": sum(1 for r in audit if r["displaced"]),
        "unschedulable_count": len(unschedulable),
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective_tuple["objective"],
    }
    write_json(out_dir, "final_state.json", final_state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
