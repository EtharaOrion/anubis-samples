#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no floating point value is ever
formed, compared or written.
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

LEN_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
CENTS_PER_UNIT = 100
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


# --------------------------------------------------------------------------
# exact arithmetic helpers
# --------------------------------------------------------------------------

def round_half_up(num, den):
    """Round num/den to an integer; a zero divisor yields the sentinel zero."""
    if den == 0:
        return 0
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


def reduced_ratio(num, den):
    """Reduced pair; a zero denominator yields the sentinel zero over one."""
    if den == 0:
        return 0, 1
    g = gcd(abs(num), abs(den))
    if g == 0:
        return 0, 1
    return num // g, den // g


def to_int(text, default=0):
    if text is None:
        return default
    text = str(text).strip()
    if text == "":
        return default
    return int(text)


def to_flag(text):
    return to_int(text, 0) != 0


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_rows(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load(data_dir):
    snap = {}
    snap["horizon"] = read_json(data_dir, "horizon_config.json")
    snap["objective_order"] = read_json(data_dir, "objective_order.json")
    # handling margins, bay interiors and crate interiors share one declared unit
    clr = LEN_FACTOR[snap["horizon"].get("clearance_unit", "MM")]

    master = []
    seen = set()
    for row in read_rows(data_dir, "object_master.csv"):
        oid = row["object_id"]
        if oid in seen:
            continue
        seen.add(oid)
        master.append(
            {
                "object_id": oid,
                "lender_id": row["lender_id"],
                "material_code": row["material_code"],
            }
        )
    snap["master"] = master

    snap["dims"] = {
        r["object_id"]: {
            "width": to_int(r["width"]),
            "height": to_int(r["height"]),
            "depth": to_int(r["depth"]),
            "dim_unit": r["dim_unit"].strip(),
        }
        for r in read_rows(data_dir, "object_dimensions.csv")
    }
    snap["mass"] = {
        r["object_id"]: (to_int(r["mass"]), r["mass_unit"].strip())
        for r in read_rows(data_dir, "object_mass.csv")
    }
    snap["value"] = {
        r["object_id"]: to_int(r["declared_value"])
        for r in read_rows(data_dir, "object_value.csv")
    }
    snap["orientation"] = {
        r["object_id"]: (to_flag(r["tip_allowed"]), to_flag(r["rotate_allowed"]))
        for r in read_rows(data_dir, "object_orientation.csv")
    }

    versions = {}
    for r in read_rows(data_dir, "object_version_map.csv"):
        oid = r["object_id"]
        v = to_int(r["version"])
        if oid not in versions or v > versions[oid]:
            versions[oid] = v
    snap["versions"] = versions

    snap["material"] = {
        r["material_code"]: {
            "conservation_class": r["conservation_class"],
            "max_lux": to_int(r["max_lux"]),
            "handling_class": r["handling_class"],
        }
        for r in read_rows(data_dir, "material_rules.csv")
    }
    snap["handling"] = {
        r["handling_class"]: {
            "clearance_w": to_int(r["clearance_w"]) * clr,
            "clearance_d": to_int(r["clearance_d"]) * clr,
            "clearance_h": to_int(r["clearance_h"]) * clr,
            "handle_margin": to_int(r["handle_margin"]) * clr,
            "crew_class": r["crew_class"],
            "setup_base": to_int(r["setup_base"]),
        }
        for r in read_rows(data_dir, "handling_class_rules.csv")
    }
    snap["bays"] = {
        r["bay_id"]: {
            "bay_id": r["bay_id"],
            "interior_w": to_int(r["interior_w"]) * clr,
            "interior_h": to_int(r["interior_h"]) * clr,
            "interior_d": to_int(r["interior_d"]) * clr,
            "lux_level": to_int(r["lux_level"]),
            "bay_priority": to_int(r["bay_priority"]),
        }
        for r in read_rows(data_dir, "gallery_bays.csv")
    }

    exceptions = {}
    for r in read_rows(data_dir, "bay_exceptions.csv"):
        exceptions.setdefault(r["bay_id"], set()).add(r["blocked_class"])
    snap["bay_exceptions"] = exceptions

    snap["crates"] = {
        r["crate_class"]: {
            "crate_class": r["crate_class"],
            "inner_w": to_int(r["inner_w"]) * clr,
            "inner_h": to_int(r["inner_h"]) * clr,
            "inner_d": to_int(r["inner_d"]) * clr,
            "count": to_int(r["count"]),
            "transport_mode": r["transport_mode"],
        }
        for r in read_rows(data_dir, "crate_catalog.csv")
    }

    reservations = {}
    for r in read_rows(data_dir, "crate_reservations.csv"):
        key = (r["crate_class"], r["lender_id"])
        reservations[key] = reservations.get(key, 0) + to_int(r["reserved_count"])
    snap["reservations"] = reservations

    prior = {}
    for r in read_rows(data_dir, "prior_state.csv"):
        cls = r["crate_class"]
        prior[cls] = prior.get(cls, 0) + to_int(r["consumed_count"])
    snap["prior"] = prior

    snap["conservation"] = {
        r["conservation_class"]: {
            "setup_start": to_int(r["setup_start"]),
            "setup_end": to_int(r["setup_end"]),
            "needs_dark": to_flag(r["needs_dark"]),
        }
        for r in read_rows(data_dir, "conservation_rules.csv")
    }
    snap["venue"] = {
        r["bay_id"]: (to_int(r["access_start"]), to_int(r["access_end"]))
        for r in read_rows(data_dir, "venue_windows.csv")
    }
    snap["crew"] = {
        r["crew_class"]: (to_int(r["avail_start"]), to_int(r["avail_end"]))
        for r in read_rows(data_dir, "crew_roster.csv")
    }
    snap["dependency_rules"] = [
        {
            "from_object": r["from_object"],
            "to_object": r["to_object"],
            "edge_priority": to_int(r["edge_priority"]),
        }
        for r in read_rows(data_dir, "dependency_rules.csv")
    ]
    snap["transport"] = {
        r["lender_id"]: (r["forbidden_mode"], r["fallback_mode"])
        for r in read_rows(data_dir, "transport_rules.csv")
    }
    snap["lenders"] = {
        r["lender_id"]: {
            "insurance_rate_num": to_int(r["insurance_rate_num"]),
            "insurance_rate_den": to_int(r["insurance_rate_den"]),
            "pref_mode": r["pref_mode"],
        }
        for r in read_rows(data_dir, "lender_master.csv")
    }
    snap["value_classes"] = [
        {
            "value_class": r["value_class"],
            "min_value": to_int(r["min_value"]),
            "unschedulable_penalty": to_int(r["unschedulable_penalty"]),
            "contention_penalty": to_int(r["contention_penalty"]),
        }
        for r in read_rows(data_dir, "value_class_rules.csv")
    ]
    return snap


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def stage_canonical(snap):
    """Stage 1: identity, units and resolved version."""
    objects = []
    for row in snap["master"]:
        oid = row["object_id"]
        dim = snap["dims"].get(
            oid, {"width": 0, "height": 0, "depth": 0, "dim_unit": "MM"}
        )
        lf = LEN_FACTOR.get(dim["dim_unit"], 0)
        mass_val, mass_unit = snap["mass"].get(oid, (0, "G"))
        mf = MASS_FACTOR.get(mass_unit, 0)
        material = snap["material"].get(
            row["material_code"],
            {"conservation_class": "", "max_lux": 0, "handling_class": ""},
        )
        handling = snap["handling"].get(
            material["handling_class"],
            {
                "clearance_w": 0,
                "clearance_d": 0,
                "clearance_h": 0,
                "handle_margin": 0,
                "crew_class": "",
                "setup_base": 0,
            },
        )
        declared = snap["value"].get(oid, 0)
        tip, rotate = snap["orientation"].get(oid, (False, False))
        objects.append(
            {
                "object_id": oid,
                "lender_id": row["lender_id"],
                "material_code": row["material_code"],
                "version": snap["versions"].get(oid, 0),
                "declared_value": declared,
                "declared_value_cents": declared * CENTS_PER_UNIT,
                "mass_g": mass_val * mf,
                "w_um": dim["width"] * lf,
                "h_um": dim["height"] * lf,
                "d_um": dim["depth"] * lf,
                "tip_allowed": tip,
                "rotate_allowed": rotate,
                "material": material,
                "handling": handling,
            }
        )
    return objects


def stage_geometry(objects):
    """Stage 2: clearance envelope and aspect ratio."""
    for obj in objects:
        h = obj["handling"]
        obj["clearance_w"] = obj["w_um"] + 2 * h["clearance_w"]
        obj["clearance_d"] = obj["d_um"] + 2 * h["clearance_d"]
        obj["clearance_h"] = obj["h_um"] + h["clearance_h"]
        num, den = reduced_ratio(obj["h_um"], obj["w_um"])
        obj["aspect_num"] = num
        obj["aspect_den"] = den


def orientation_footprint(obj, index):
    """(footprint width, footprint depth, vertical extent) of an orientation."""
    cw, cd, ch = obj["clearance_w"], obj["clearance_d"], obj["clearance_h"]
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def stage_admissibility(objects):
    """Stage 3: the admissible orientation list."""
    for obj in objects:
        orientations = [0]
        if obj["rotate_allowed"]:
            orientations.append(1)
        if obj["tip_allowed"]:
            orientations.extend([2, 3])
        obj["orientations"] = orientations


def fits_bay(obj, orientation, bay):
    fw, fd, fv = orientation_footprint(obj, orientation)
    return (
        fw <= bay["interior_w"]
        and fd < bay["interior_d"]
        and fv <= bay["interior_h"]
    )


def fitting_orientation(obj, bay):
    for index in obj["orientations"]:
        if fits_bay(obj, index, bay):
            return index
    return -1


def bay_scan_order(snap):
    return sorted(
        snap["bays"].values(), key=lambda b: (b["bay_priority"], b["bay_id"])
    )


def stage_bay_candidates(objects, snap):
    """Stage 4: the per object per bay fit matrix."""
    rows = []
    for obj in objects:
        obj["fits_by_bay"] = {}
        for bay_id in sorted(snap["bays"]):
            index = fitting_orientation(obj, snap["bays"][bay_id])
            obj["fits_by_bay"][bay_id] = index
            rows.append(
                {
                    "object_id": obj["object_id"],
                    "bay_id": bay_id,
                    "fits": index >= 0,
                    "orientation": index,
                }
            )
    return rows


def reject_code(obj):
    return "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"


def stage_selected_placement(objects, snap):
    """Stage 5: geometry only placement."""
    rows = []
    for obj in objects:
        chosen_bay, chosen_index = "", -1
        for bay in bay_scan_order(snap):
            index = obj["fits_by_bay"][bay["bay_id"]]
            if index >= 0:
                chosen_bay, chosen_index = bay["bay_id"], index
                break
        obj["selected_bay"] = chosen_bay
        obj["selected_orientation"] = chosen_index
        obj["selected_code"] = "P_OK" if chosen_bay else reject_code(obj)
        rows.append(
            {
                "object_id": obj["object_id"],
                "bay_id": chosen_bay,
                "orientation": chosen_index,
                "code": obj["selected_code"],
            }
        )
    return rows


def conservation_blocks(obj, bay, snap):
    """Why a bay is excluded for an object, or None when it is allowed."""
    material = obj["material"]
    cls = material["conservation_class"]
    rule = snap["conservation"].get(cls)
    if bay["lux_level"] > material["max_lux"]:
        return "LUX"
    if cls in snap["bay_exceptions"].get(bay["bay_id"], set()):
        return "EXCEPTION"
    if rule is not None and rule["needs_dark"] and bay["lux_level"] > 0:
        return "DARK"
    return None


def stage_conservation(objects, snap):
    """Stage 6: placement that also honours conservation."""
    rows = []
    for obj in objects:
        chosen_bay, chosen_index = "", -1
        geometric = []
        blocked_by_exception = []
        for bay in bay_scan_order(snap):
            index = obj["fits_by_bay"][bay["bay_id"]]
            if index < 0:
                continue
            geometric.append(bay["bay_id"])
            reason = conservation_blocks(obj, bay, snap)
            if reason is None:
                if not chosen_bay:
                    chosen_bay, chosen_index = bay["bay_id"], index
            elif reason == "EXCEPTION":
                blocked_by_exception.append(bay["bay_id"])
        if chosen_bay:
            code = "P_OK"
        elif not geometric:
            code = reject_code(obj)
        elif len(geometric) == 1 and blocked_by_exception == geometric:
            code = "P_BAY_EXCEPTION"
        else:
            code = "P_CONSERVATION_BLOCK"
        obj["bay"] = chosen_bay
        obj["orientation"] = chosen_index
        obj["placement_code"] = code
        rows.append(
            {
                "object_id": obj["object_id"],
                "bay_id": chosen_bay,
                "orientation": chosen_index,
                "code": code,
            }
        )
    return rows


def stage_crate_candidates(objects, snap):
    """Stage 7: handling envelope, fitting crates and their residuals."""
    rows = []
    for obj in sorted(objects, key=lambda o: o["object_id"]):
        margin = obj["handling"]["handle_margin"]
        hw = obj["w_um"] + margin
        hh = obj["h_um"] + margin
        hd = obj["d_um"] + margin
        obj["handling_volume"] = hw * hh * hd
        handling_sorted = sorted([hw, hh, hd])
        candidates = []
        for crate_class in sorted(snap["crates"]):
            crate = snap["crates"][crate_class]
            inner_sorted = sorted(
                [crate["inner_w"], crate["inner_h"], crate["inner_d"]]
            )
            if all(a <= b for a, b in zip(handling_sorted, inner_sorted)):
                residual = (
                    inner_sorted[0] * inner_sorted[1] * inner_sorted[2]
                    - obj["handling_volume"]
                )
                candidates.append((residual, crate_class))
        candidates.sort(key=lambda item: (item[0], item[1]))
        obj["crate_candidates"] = candidates
        obj["primary_class"] = candidates[0][1] if candidates else ""
        for residual, crate_class in candidates:
            rows.append(
                {
                    "object_id": obj["object_id"],
                    "crate_class": crate_class,
                    "residual": residual,
                }
            )
    return rows


def stage_crate_assignment(objects, snap, install_order):
    """Stage 8: consume the shared crate stock in install order."""
    remaining = {}
    for crate_class, crate in snap["crates"].items():
        remaining[crate_class] = crate["count"] - snap["prior"].get(crate_class, 0)
    reservations = dict(snap["reservations"])
    by_id = {obj["object_id"]: obj for obj in objects}

    for oid in install_order:
        obj = by_id[oid]
        options = []
        for residual, crate_class in obj["crate_candidates"]:
            if remaining.get(crate_class, 0) <= 0:
                continue
            reserved = reservations.get((crate_class, obj["lender_id"]), 0) > 0
            options.append((0 if reserved else 1, residual, crate_class, reserved))
        if not options:
            obj["crate_class"] = ""
            obj["crate_code"] = "C_NO_CRATE"
            obj["residual"] = 0
            continue
        options.sort(key=lambda item: (item[0], item[1], item[2]))
        _, residual, crate_class, reserved = options[0]
        remaining[crate_class] -= 1
        if reserved:
            reservations[(crate_class, obj["lender_id"])] -= 1
        obj["crate_class"] = crate_class
        obj["residual"] = residual
        obj["crate_code"] = (
            "C_PRIMARY" if crate_class == obj["primary_class"] else "C_DISPLACED"
        )

    assignment_rows = []
    contention_rows = []
    for obj in sorted(objects, key=lambda o: o["object_id"]):
        assignment_rows.append(
            {
                "object_id": obj["object_id"],
                "crate_class": obj["crate_class"],
                "code": obj["crate_code"],
                "primary_class": obj["primary_class"],
            }
        )
        if obj["crate_code"] == "C_DISPLACED":
            contention_rows.append(
                {
                    "object_id": obj["object_id"],
                    "wanted_class": obj["primary_class"],
                    "assigned_class": obj["crate_class"],
                }
            )
    ledger_rows = [
        {
            "crate_class": crate_class,
            "catalog_count": snap["crates"][crate_class]["count"],
            "prior_consumed": snap["prior"].get(crate_class, 0),
            "remaining": remaining[crate_class],
        }
        for crate_class in sorted(snap["crates"])
    ]
    return assignment_rows, contention_rows, ledger_rows


def stage_transport(objects, snap):
    """Stage 9: the transport leg of every crated object."""
    rows = []
    for obj in objects:
        if not obj["crate_class"]:
            continue
        mode = snap["crates"][obj["crate_class"]]["transport_mode"]
        exception = False
        rule = snap["transport"].get(obj["lender_id"])
        if rule is not None and rule[0] == mode:
            mode = rule[1]
            exception = True
        rows.append(
            {
                "object_id": obj["object_id"],
                "crate_class": obj["crate_class"],
                "mode": mode,
                "exception": exception,
            }
        )
    return rows


def stage_dependency_edges(objects, snap):
    """Stage 10: the directed install dependency graph."""
    edges = {}

    def add(frm, to, kind, weight):
        if frm == to:
            return
        key = (frm, to)
        current = edges.get(key)
        if current is None or KIND_RANK[kind] > KIND_RANK[current["kind"]]:
            edges[key] = {"kind": kind, "weight": weight}

    for rule in snap["dependency_rules"]:
        add(rule["from_object"], rule["to_object"], KIND_RULE, rule["edge_priority"])

    by_bay = {}
    for obj in objects:
        if obj["placement_code"] == "P_OK":
            by_bay.setdefault(obj["bay"], []).append(obj)

    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id], key=lambda o: o["object_id"])
        for a in members:
            for b in members:
                if a["object_id"] >= b["object_id"]:
                    continue
                # larger handling volume installs first, ties by lower object id
                if (a["handling_volume"], b["object_id"]) > (
                    b["handling_volume"],
                    a["object_id"],
                ):
                    big, small = a, b
                else:
                    big, small = b, a
                add(
                    big["object_id"],
                    small["object_id"],
                    KIND_BAYSIZE,
                    big["handling_volume"],
                )
        for a in members:
            for b in members:
                if a["object_id"] == b["object_id"]:
                    continue
                if (
                    a["material"]["conservation_class"]
                    > b["material"]["conservation_class"]
                ):
                    add(
                        a["object_id"],
                        b["object_id"],
                        KIND_CONSERVATION,
                        a["handling_volume"],
                    )
    return edges


def find_cycle(nodes, edges):
    """The first cycle a depth first search in ascending id order meets."""
    adjacency = {node: [] for node in nodes}
    for frm, to in edges:
        adjacency[frm].append(to)
    for node in adjacency:
        adjacency[node].sort()

    color = {node: 0 for node in nodes}
    for root in sorted(nodes):
        if color[root] != 0:
            continue
        path = [root]
        on_path = {root}
        color[root] = 1
        stack = [(root, iter(adjacency[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt in on_path:
                    return path[path.index(nxt):]
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    on_path.add(nxt)
                    stack.append((nxt, iter(adjacency[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                on_path.discard(node)
                path.pop()
                stack.pop()
    return None


def stage_cycle_resolution(nodes, edges):
    """Stage 11: break every cycle by removing its lowest priority edge."""
    removed = []
    while True:
        cycle = find_cycle(nodes, edges)
        if cycle is None:
            break
        pairs = [(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))]
        victim = min(
            pairs,
            key=lambda p: (KIND_RANK[edges[p]["kind"]], edges[p]["weight"], p[0], p[1]),
        )
        data = edges.pop(victim)
        removed.append(
            {
                "from": victim[0],
                "to": victim[1],
                "kind": data["kind"],
                "weight": data["weight"],
            }
        )
    return removed


def stage_crew(objects, snap):
    """Stage 13 crew part: crew class and rounded setup seconds."""
    setup_div = to_int(snap["horizon"].get("setup_div"), 0)
    for obj in objects:
        handling = obj["handling"]
        obj["crew_class"] = handling["crew_class"]
        obj["setup_seconds"] = round_half_up(
            handling["setup_base"] * obj["mass_g"], setup_div
        )


def stage_install_order(nodes, edges, setup_seconds):
    """Stage 12: repeated ready set selection over the acyclic graph."""
    indegree = {node: 0 for node in nodes}
    successors = {node: [] for node in nodes}
    for (frm, to) in edges:
        indegree[to] += 1
        successors[frm].append(to)
    ready = [node for node in nodes if indegree[node] == 0]
    order = []
    while ready:
        ready.sort(key=lambda n: (setup_seconds[n], n))
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(successors[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    return order


def stage_windows(objects, snap):
    """Stage 13 window part: intersect the three windows and schedule."""
    horizon = snap["horizon"]
    venue_shift = 1 if horizon.get("venue_end_convention", "exclusive") == "exclusive" else 0
    conservation_shift = (
        1 if horizon.get("conservation_end_convention", "inclusive") == "exclusive" else 0
    )
    rows = []
    for obj in objects:
        required_days = ceil_div(obj["setup_seconds"], SECONDS_PER_DAY)
        bay_id = obj["bay"]
        if obj["placement_code"] != "P_OK":
            blocked = "S_NO_PLACEMENT"
        elif not obj["crate_class"]:
            blocked = "S_NO_CRATE"
        else:
            blocked = None
        if blocked is not None:
            obj["window_start"] = 0
            obj["window_end"] = -1
            obj["required_days"] = required_days
            obj["schedule_code"] = blocked
            obj["scheduled"] = False
            rows.append(
                {
                    "object_id": obj["object_id"],
                    "bay_id": bay_id,
                    "window_start": 0,
                    "window_end": -1,
                    "required_days": required_days,
                    "code": blocked,
                }
            )
            continue

        starts = []
        ends = []
        rule = snap["conservation"].get(obj["material"]["conservation_class"])
        if rule is not None:
            starts.append(rule["setup_start"])
            ends.append(rule["setup_end"] - conservation_shift)
        venue = snap["venue"].get(bay_id)
        if venue is not None:
            starts.append(venue[0])
            ends.append(venue[1] - venue_shift)
        crew = snap["crew"].get(obj["crew_class"])
        if crew is not None:
            starts.append(crew[0])
            ends.append(crew[1])
        low = max(starts) if starts else 0
        high = min(ends) if ends else -1
        available_days = high - low + 1
        if available_days <= 0:
            code = "S_NO_WINDOW"
        elif required_days > available_days:
            code = "S_SHORT_WINDOW"
        else:
            code = "S_OK"
        obj["window_start"] = low
        obj["window_end"] = high
        obj["required_days"] = required_days
        obj["schedule_code"] = code
        obj["scheduled"] = code == "S_OK"
        rows.append(
            {
                "object_id": obj["object_id"],
                "bay_id": bay_id,
                "window_start": low,
                "window_end": high,
                "required_days": required_days,
                "code": code,
            }
        )
    return rows


def value_class_of(obj, snap):
    """The highest value class whose minimum the declared value meets."""
    best = None
    for entry in snap["value_classes"]:
        if obj["declared_value"] >= entry["min_value"]:
            if best is None or entry["min_value"] > best["min_value"]:
                best = entry
    return best


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------

def write_lines(out_dir, name, rows):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def write_object(out_dir, name, value):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(value, sort_keys=True, indent=2))
        fh.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def run(data_dir, out_dir):
    snap = load(data_dir)
    objects = stage_canonical(snap)
    stage_geometry(objects)
    stage_admissibility(objects)

    bay_candidate_rows = stage_bay_candidates(objects, snap)
    selected_rows = stage_selected_placement(objects, snap)
    rejected_rows = sorted(
        (
            {"object_id": r["object_id"], "code": r["code"]}
            for r in selected_rows
            if r["code"] != "P_OK"
        ),
        key=lambda r: (r["object_id"], r["code"]),
    )
    conservation_rows = stage_conservation(objects, snap)
    crate_candidate_rows = stage_crate_candidates(objects, snap)

    stage_crew(objects, snap)
    setup_seconds = {obj["object_id"]: obj["setup_seconds"] for obj in objects}
    nodes = [obj["object_id"] for obj in objects]

    edges = stage_dependency_edges(objects, snap)
    edge_rows = [
        {"from": frm, "to": to, "kind": data["kind"], "weight": data["weight"]}
        for (frm, to), data in sorted(edges.items())
    ]
    removed_rows = stage_cycle_resolution(nodes, edges)
    install_order = stage_install_order(nodes, edges, setup_seconds)
    position_of = {oid: i + 1 for i, oid in enumerate(install_order)}

    assignment_rows, contention_rows, ledger_rows = stage_crate_assignment(
        objects, snap, install_order
    )
    transport_rows = stage_transport(objects, snap)
    window_rows = stage_windows(objects, snap)
    unschedulable_rows = sorted(
        (
            {"object_id": r["object_id"], "code": r["code"]}
            for r in window_rows
            if r["code"] != "S_OK"
        ),
        key=lambda r: (r["object_id"], r["code"]),
    )

    occupancy_rows = []
    for bay_id in sorted(snap["bays"]):
        members = sorted(
            obj["object_id"]
            for obj in objects
            if obj["placement_code"] == "P_OK" and obj["bay"] == bay_id
        )
        occupancy_rows.append(
            {"bay_id": bay_id, "object_count": len(members), "objects": members}
        )

    total_insurance = 0
    total_setup = 0
    total_residual = 0
    unschedulable_penalty = 0
    contention_penalty = 0
    audit_rows = []
    for obj in objects:
        lender = snap["lenders"].get(
            obj["lender_id"], {"insurance_rate_num": 0, "insurance_rate_den": 0}
        )
        insurance = round_half_up(
            obj["declared_value_cents"] * lender["insurance_rate_num"],
            lender["insurance_rate_den"],
        )
        total_insurance += insurance
        total_setup += obj["setup_seconds"]
        total_residual += obj["residual"]
        entry = value_class_of(obj, snap)
        if not obj["scheduled"] and entry is not None:
            unschedulable_penalty += entry["unschedulable_penalty"]
        if obj["crate_code"] == "C_DISPLACED" and entry is not None:
            contention_penalty += entry["contention_penalty"]
        audit_rows.append(
            {
                "object_id": obj["object_id"],
                "placed_bay": obj["bay"],
                "crate_class": obj["crate_class"],
                "displaced": obj["crate_code"] == "C_DISPLACED",
                "scheduled": obj["scheduled"],
                "install_position": position_of[obj["object_id"]],
                "setup_seconds": obj["setup_seconds"],
                "insurance_cents": insurance,
            }
        )

    placed_objects = sum(1 for obj in objects if obj["scheduled"])
    displacement_count = sum(1 for obj in objects if obj["crate_code"] == "C_DISPLACED")
    unschedulable_count = len(unschedulable_rows)
    cycles_broken = len(removed_rows)
    objective = [
        placed_objects,
        unschedulable_penalty,
        contention_penalty,
        cycles_broken,
        total_residual,
    ]

    write_lines(out_dir, "canonical_objects.jsonl", [
        {
            "object_id": obj["object_id"],
            "lender_id": obj["lender_id"],
            "material_code": obj["material_code"],
            "version": obj["version"],
            "declared_value_cents": obj["declared_value_cents"],
            "mass_g": obj["mass_g"],
        }
        for obj in objects
    ])
    write_lines(out_dir, "normalized_geometry.jsonl", [
        {
            "object_id": obj["object_id"],
            "clearance_w": obj["clearance_w"],
            "clearance_h": obj["clearance_h"],
            "clearance_d": obj["clearance_d"],
            "aspect_num": obj["aspect_num"],
            "aspect_den": obj["aspect_den"],
        }
        for obj in objects
    ])
    write_lines(out_dir, "admissibility_matrix.jsonl", [
        {
            "object_id": obj["object_id"],
            "orientations": obj["orientations"],
            "tip_allowed": obj["tip_allowed"],
            "rotate_allowed": obj["rotate_allowed"],
        }
        for obj in objects
    ])
    write_lines(out_dir, "bay_candidate_matrix.jsonl", bay_candidate_rows)
    write_lines(out_dir, "selected_placement.jsonl", selected_rows)
    write_lines(out_dir, "rejected_placement.jsonl", rejected_rows)
    write_lines(out_dir, "conservation_adjusted.jsonl", conservation_rows)
    write_lines(out_dir, "crate_candidate_matrix.jsonl", crate_candidate_rows)
    write_lines(out_dir, "crate_assignment.jsonl", assignment_rows)
    write_lines(out_dir, "crate_capacity_ledger.jsonl", ledger_rows)
    write_lines(out_dir, "crate_contention_report.jsonl", contention_rows)
    write_lines(out_dir, "transport_leg_manifest.jsonl", transport_rows)
    write_lines(out_dir, "dependency_edges.jsonl", edge_rows)
    write_lines(out_dir, "cycle_resolution_audit.jsonl", removed_rows)
    write_lines(out_dir, "install_order.jsonl", [
        {"position": position_of[oid], "object_id": oid} for oid in install_order
    ])
    write_lines(out_dir, "crew_requirement_ledger.jsonl", [
        {
            "object_id": obj["object_id"],
            "crew_class": obj["crew_class"],
            "setup_seconds": obj["setup_seconds"],
        }
        for obj in objects
    ])
    write_lines(out_dir, "install_window_table.jsonl", window_rows)
    write_lines(out_dir, "unschedulable_report.jsonl", unschedulable_rows)
    write_lines(out_dir, "gallery_occupancy_map.jsonl", occupancy_rows)
    write_lines(out_dir, "deterministic_audit.jsonl", audit_rows)
    write_object(out_dir, "penalty_summary.json", {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })
    write_object(out_dir, "objective_tuple.json", {
        "objective": objective,
        "order": list(snap["objective_order"]["order"]),
    })

    program_path = os.path.abspath(__file__)
    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_object(out_dir, "integrity_manifest.json", manifest)

    chain = snap["horizon"].get("chain_seed", "0" * 64)
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_object(out_dir, "final_state.json", {
        "chain": chain,
        "repair_converged": True,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": 0,
        "displacement_count": displacement_count,
        "unschedulable_count": unschedulable_count,
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    os.makedirs(out_dir, exist_ok=True)
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
