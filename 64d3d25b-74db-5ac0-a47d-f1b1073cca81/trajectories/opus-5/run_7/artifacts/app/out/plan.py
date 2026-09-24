#!/usr/bin/env python3
"""Rebuild the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is formed, compared or
written.  Lengths are micrometres, mass grams, money cents, time integer day
indices and whole seconds.
"""

import csv
import hashlib
import json
import os
import re
import sys
from math import gcd

LEN_FACTOR = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTOR = {"G": 1, "KG": 1000}
MM = 1000
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
# helpers
# --------------------------------------------------------------------------

def read_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def to_int(text, default=0):
    if text is None:
        return default
    text = str(text).strip()
    if text == "":
        return default
    return int(text)


def round_half_up(num, den):
    """Exact rational rounded half up; a zero divisor yields zero."""
    if den == 0:
        return 0
    if den < 0:
        num, den = -num, -den
    quotient, remainder = divmod(num, den)
    if 2 * remainder >= den:
        quotient += 1
    return quotient


def ceil_div(num, den):
    if den == 0:
        return 0
    quotient, remainder = divmod(num, den)
    if remainder:
        quotient += 1
    return quotient


def ratio(num, den):
    """Reduced pair; a zero denominator gives the sentinel zero over one."""
    if den == 0:
        return 0, 1
    common = gcd(abs(num), abs(den))
    if common == 0:
        return 0, 1
    return num // common, den // common


def natural_key(text):
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", text)
        if part != ""
    )


def write_jsonl(out_dir, name, rows):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_json(out_dir, name, value):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# input loading
# --------------------------------------------------------------------------

def load(data_dir):
    cfg = read_json(data_dir, "horizon_config.json")
    setup_div = to_int(cfg.get("setup_div", 700), 700)
    chain_seed = cfg.get("chain_seed", "0" * 64)

    order_cfg = read_json(data_dir, "objective_order.json")

    master = []
    seen = set()
    for row in read_csv(data_dir, "object_master.csv"):
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

    versions = {}
    for row in read_csv(data_dir, "object_version_map.csv"):
        oid = row["object_id"]
        ver = to_int(row["version"])
        if oid not in versions or ver > versions[oid]:
            versions[oid] = ver

    dims = {}
    for row in read_csv(data_dir, "object_dimensions.csv"):
        factor = LEN_FACTOR.get(row["dim_unit"].strip(), 0)
        dims[row["object_id"]] = (
            to_int(row["width"]) * factor,
            to_int(row["height"]) * factor,
            to_int(row["depth"]) * factor,
        )

    mass = {}
    for row in read_csv(data_dir, "object_mass.csv"):
        factor = MASS_FACTOR.get(row["mass_unit"].strip(), 0)
        mass[row["object_id"]] = to_int(row["mass"]) * factor

    value = {
        row["object_id"]: to_int(row["declared_value"])
        for row in read_csv(data_dir, "object_value.csv")
    }

    orient = {
        row["object_id"]: (to_int(row["tip_allowed"]), to_int(row["rotate_allowed"]))
        for row in read_csv(data_dir, "object_orientation.csv")
    }

    materials = {
        row["material_code"]: {
            "conservation_class": row["conservation_class"],
            "max_lux": to_int(row["max_lux"]),
            "handling_class": row["handling_class"],
        }
        for row in read_csv(data_dir, "material_rules.csv")
    }

    handling = {
        row["handling_class"]: {
            "clearance_w": to_int(row["clearance_w"]),
            "clearance_d": to_int(row["clearance_d"]),
            "clearance_h": to_int(row["clearance_h"]),
            "handle_margin": to_int(row["handle_margin"]),
            "crew_class": row["crew_class"],
            "setup_base": to_int(row["setup_base"]),
        }
        for row in read_csv(data_dir, "handling_class_rules.csv")
    }

    bays = {}
    for row in read_csv(data_dir, "gallery_bays.csv"):
        bays[row["bay_id"]] = {
            "bay_id": row["bay_id"],
            "interior_w": to_int(row["interior_w"]) * MM,
            "interior_h": to_int(row["interior_h"]) * MM,
            "interior_d": to_int(row["interior_d"]) * MM,
            "lux_level": to_int(row["lux_level"]),
            "bay_priority": to_int(row["bay_priority"]),
        }

    bay_exceptions = {}
    for row in read_csv(data_dir, "bay_exceptions.csv"):
        bay_exceptions.setdefault(row["bay_id"], set()).add(row["blocked_class"])

    crates = {}
    for row in read_csv(data_dir, "crate_catalog.csv"):
        crates[row["crate_class"]] = {
            "crate_class": row["crate_class"],
            "inner_w": to_int(row["inner_w"]) * MM,
            "inner_h": to_int(row["inner_h"]) * MM,
            "inner_d": to_int(row["inner_d"]) * MM,
            "count": to_int(row["count"]),
            "transport_mode": row["transport_mode"],
        }

    reservations = {}
    for row in read_csv(data_dir, "crate_reservations.csv"):
        key = (row["crate_class"], row["lender_id"])
        reservations[key] = reservations.get(key, 0) + to_int(row["reserved_count"])

    prior = {}
    for row in read_csv(data_dir, "prior_state.csv"):
        cls = row["crate_class"]
        prior[cls] = prior.get(cls, 0) + to_int(row["consumed_count"])

    conservation = {
        row["conservation_class"]: {
            "setup_start": to_int(row["setup_start"]),
            "setup_end": to_int(row["setup_end"]),
            "needs_dark": to_int(row["needs_dark"]),
        }
        for row in read_csv(data_dir, "conservation_rules.csv")
    }

    venue = {
        row["bay_id"]: (to_int(row["access_start"]), to_int(row["access_end"]))
        for row in read_csv(data_dir, "venue_windows.csv")
    }

    dep_rules = [
        (row["from_object"], row["to_object"], to_int(row["edge_priority"]))
        for row in read_csv(data_dir, "dependency_rules.csv")
    ]

    crew = {
        row["crew_class"]: (to_int(row["avail_start"]), to_int(row["avail_end"]))
        for row in read_csv(data_dir, "crew_roster.csv")
    }

    transport = {
        row["lender_id"]: (row["forbidden_mode"], row["fallback_mode"])
        for row in read_csv(data_dir, "transport_rules.csv")
    }

    lenders = {
        row["lender_id"]: {
            "insurance_rate_num": to_int(row["insurance_rate_num"]),
            "insurance_rate_den": to_int(row["insurance_rate_den"]),
            "pref_mode": row["pref_mode"],
        }
        for row in read_csv(data_dir, "lender_master.csv")
    }

    value_classes = [
        {
            "value_class": row["value_class"],
            "min_value": to_int(row["min_value"]),
            "unschedulable_penalty": to_int(row["unschedulable_penalty"]),
            "contention_penalty": to_int(row["contention_penalty"]),
        }
        for row in read_csv(data_dir, "value_class_rules.csv")
    ]

    return {
        "setup_div": setup_div,
        "chain_seed": chain_seed,
        "objective_order": order_cfg.get("order", []),
        "master": master,
        "versions": versions,
        "dims": dims,
        "mass": mass,
        "value": value,
        "orient": orient,
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


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def orientation_shapes(clearance_w, clearance_h, clearance_d):
    """Footprint width, footprint depth and vertical extent per orientation."""
    return {
        0: (clearance_w, clearance_d, clearance_h),
        1: (clearance_d, clearance_w, clearance_h),
        2: (clearance_w, clearance_h, clearance_d),
        3: (clearance_h, clearance_w, clearance_d),
    }


def fits_bay(shape, bay):
    foot_w, foot_d, vertical = shape
    return (
        foot_w <= bay["interior_w"]
        and foot_d < bay["interior_d"]
        and vertical <= bay["interior_h"]
    )


def first_fitting_orientation(obj, bay):
    for index in obj["orientations"]:
        if fits_bay(obj["shapes"][index], bay):
            return index
    return None


def build_objects(data):
    objects = []
    for row in data["master"]:
        oid = row["object_id"]
        material = data["materials"].get(row["material_code"], {})
        handling_class = material.get("handling_class", "")
        handling = data["handling"].get(handling_class, {})
        width, height, depth = data["dims"].get(oid, (0, 0, 0))
        tip, rotate = data["orient"].get(oid, (0, 0))

        clearance_w = width + 2 * handling.get("clearance_w", 0) * MM
        clearance_d = depth + 2 * handling.get("clearance_d", 0) * MM
        clearance_h = height + handling.get("clearance_h", 0) * MM
        aspect_num, aspect_den = ratio(height, width)

        orientations = [0]
        if rotate:
            orientations.append(1)
        if tip:
            orientations.extend([2, 3])

        margin = handling.get("handle_margin", 0) * MM
        handle_w = width + margin
        handle_h = height + margin
        handle_d = depth + margin

        objects.append(
            {
                "object_id": oid,
                "lender_id": row["lender_id"],
                "material_code": row["material_code"],
                "version": data["versions"].get(oid, 0),
                "declared_value": data["value"].get(oid, 0),
                "declared_value_cents": data["value"].get(oid, 0) * CENTS_PER_UNIT,
                "mass_g": data["mass"].get(oid, 0),
                "width": width,
                "height": height,
                "depth": depth,
                "tip_allowed": tip,
                "rotate_allowed": rotate,
                "clearance_w": clearance_w,
                "clearance_h": clearance_h,
                "clearance_d": clearance_d,
                "aspect_num": aspect_num,
                "aspect_den": aspect_den,
                "orientations": orientations,
                "shapes": orientation_shapes(clearance_w, clearance_h, clearance_d),
                "conservation_class": material.get("conservation_class", ""),
                "max_lux": material.get("max_lux", 0),
                "handling_class": handling_class,
                "crew_class": handling.get("crew_class", ""),
                "setup_base": handling.get("setup_base", 0),
                "handle_dims": (handle_w, handle_h, handle_d),
                "handle_volume": handle_w * handle_h * handle_d,
            }
        )
    return objects


def bay_scan_order(data):
    return sorted(
        data["bays"].values(), key=lambda bay: (bay["bay_priority"], bay["bay_id"])
    )


def conservation_ok(obj, bay, data):
    if bay["lux_level"] > obj["max_lux"]:
        return False
    if obj["conservation_class"] in data["bay_exceptions"].get(bay["bay_id"], set()):
        return False
    rule = data["conservation"].get(obj["conservation_class"], {})
    if rule.get("needs_dark", 0) and bay["lux_level"] > 0:
        return False
    return True


def excluded_by_bay_exception(obj, bay, data):
    return obj["conservation_class"] in data["bay_exceptions"].get(
        bay["bay_id"], set()
    )


def value_class_of(obj, data):
    for entry in sorted(
        data["value_classes"], key=lambda e: (-e["min_value"], e["value_class"])
    ):
        if obj["declared_value"] >= entry["min_value"]:
            return entry
    return None


def find_cycle(nodes, adjacency):
    """First cycle under a deterministic depth first scan, as a node path."""
    color = {node: 0 for node in nodes}
    for start in nodes:
        if color[start] != 0:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(adjacency[start]))]
        while stack:
            node, iterator = stack[-1]
            advanced = False
            for nxt in iterator:
                if color[nxt] == 1:
                    index = path.index(nxt)
                    return path[index:] + [nxt]
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(adjacency[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                path.pop()
                stack.pop()
    return None


def build_adjacency(nodes, edges):
    adjacency = {node: [] for node in nodes}
    for (src, dst) in sorted(edges):
        adjacency[src].append(dst)
    return adjacency


def derive(data):
    state = {}
    objects = build_objects(data)
    by_id = {obj["object_id"]: obj for obj in objects}
    ids_sorted = sorted(by_id)
    state["objects"] = objects
    state["by_id"] = by_id

    bays_ordered = bay_scan_order(data)
    bays_by_id = sorted(data["bays"].values(), key=lambda bay: bay["bay_id"])

    # stage 4 bay candidates -------------------------------------------------
    bay_candidates = []
    geometric_fit = {}
    for obj in objects:
        fitting = {}
        for bay in bays_by_id:
            index = first_fitting_orientation(obj, bay)
            bay_candidates.append(
                {
                    "object_id": obj["object_id"],
                    "bay_id": bay["bay_id"],
                    "fits": index is not None,
                    "orientation": -1 if index is None else index,
                }
            )
            if index is not None:
                fitting[bay["bay_id"]] = index
        geometric_fit[obj["object_id"]] = fitting
    state["bay_candidates"] = bay_candidates

    # stage 5 selected placement --------------------------------------------
    selected = {}
    for obj in objects:
        oid = obj["object_id"]
        chosen = None
        for bay in bays_ordered:
            if bay["bay_id"] in geometric_fit[oid]:
                chosen = (bay["bay_id"], geometric_fit[oid][bay["bay_id"]])
                break
        if chosen is None:
            code = "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"
            selected[oid] = {"bay_id": "", "orientation": -1, "code": code}
        else:
            selected[oid] = {
                "bay_id": chosen[0],
                "orientation": chosen[1],
                "code": "P_OK",
            }
    state["selected"] = selected

    # stage 6 conservation adjusted placement --------------------------------
    adjusted = {}
    for obj in objects:
        oid = obj["object_id"]
        chosen = None
        for bay in bays_ordered:
            if bay["bay_id"] not in geometric_fit[oid]:
                continue
            if not conservation_ok(obj, bay, data):
                continue
            chosen = (bay["bay_id"], geometric_fit[oid][bay["bay_id"]])
            break
        if chosen is not None:
            adjusted[oid] = {
                "bay_id": chosen[0],
                "orientation": chosen[1],
                "code": "P_OK",
            }
            continue
        if not geometric_fit[oid]:
            code = selected[oid]["code"]
        else:
            fitting_bays = [
                bay for bay in bays_ordered if bay["bay_id"] in geometric_fit[oid]
            ]
            if len(fitting_bays) == 1 and excluded_by_bay_exception(
                obj, fitting_bays[0], data
            ):
                code = "P_BAY_EXCEPTION"
            else:
                code = "P_CONSERVATION_BLOCK"
        adjusted[oid] = {"bay_id": "", "orientation": -1, "code": code}
    state["adjusted"] = adjusted

    rejected = set()
    for oid in ids_sorted:
        if selected[oid]["code"] != "P_OK":
            rejected.add((oid, selected[oid]["code"]))
        if adjusted[oid]["code"] != "P_OK":
            rejected.add((oid, adjusted[oid]["code"]))
    state["rejected"] = sorted(rejected)

    # stage 7 crate candidates ----------------------------------------------
    crate_candidates = {}
    primary_crate = {}
    for obj in objects:
        handle_sorted = sorted(obj["handle_dims"])
        rows = []
        for cls in sorted(data["crates"]):
            crate = data["crates"][cls]
            inner_sorted = sorted((crate["inner_w"], crate["inner_h"], crate["inner_d"]))
            if all(h <= i for h, i in zip(handle_sorted, inner_sorted)):
                volume = crate["inner_w"] * crate["inner_h"] * crate["inner_d"]
                rows.append((cls, volume - obj["handle_volume"]))
        rows.sort(key=lambda item: (item[1], item[0]))
        crate_candidates[obj["object_id"]] = rows
        primary_crate[obj["object_id"]] = rows[0][0] if rows else ""
    state["crate_candidates"] = crate_candidates
    state["primary_crate"] = primary_crate

    # stage 13 setup seconds, consumed by the install order of stage 12 ------
    setup_seconds = {
        obj["object_id"]: round_half_up(
            obj["setup_base"] * obj["mass_g"], data["setup_div"]
        )
        for obj in objects
    }
    state["setup_seconds"] = setup_seconds

    # stage 10 dependency edges ---------------------------------------------
    edges = {}

    def offer(src, dst, kind, weight):
        key = (src, dst)
        rank = (KIND_RANK[kind], weight)
        existing = edges.get(key)
        if existing is None or rank > (KIND_RANK[existing["kind"]], existing["weight"]):
            edges[key] = {"from": src, "to": dst, "kind": kind, "weight": weight}

    by_bay = {}
    for oid in ids_sorted:
        bay_id = adjusted[oid]["bay_id"]
        if bay_id:
            by_bay.setdefault(bay_id, []).append(oid)

    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id])
        for index, first_id in enumerate(members):
            for second_id in members[index + 1 :]:
                obj_a, obj_b = by_id[first_id], by_id[second_id]
                if obj_a["handle_volume"] >= obj_b["handle_volume"]:
                    earlier, later = obj_a, obj_b
                else:
                    earlier, later = obj_b, obj_a
                offer(
                    earlier["object_id"],
                    later["object_id"],
                    KIND_BAYSIZE,
                    earlier["handle_volume"],
                )
                key_a = natural_key(obj_a["conservation_class"])
                key_b = natural_key(obj_b["conservation_class"])
                if key_a != key_b:
                    earlier, later = (obj_a, obj_b) if key_a > key_b else (obj_b, obj_a)
                    offer(
                        earlier["object_id"],
                        later["object_id"],
                        KIND_CONSERVATION,
                        earlier["handle_volume"],
                    )

    for src, dst, priority in data["dep_rules"]:
        if src in by_id and dst in by_id:
            offer(src, dst, KIND_RULE, priority)

    state["edges"] = edges

    # stage 11 cycle resolution ---------------------------------------------
    remaining = dict(edges)
    removed = []
    converged = False
    for _ in range(len(edges) + 1):
        cycle = find_cycle(ids_sorted, build_adjacency(ids_sorted, remaining))
        if cycle is None:
            converged = True
            break
        cycle_edges = [
            remaining[(cycle[i], cycle[i + 1])] for i in range(len(cycle) - 1)
        ]
        victim = min(
            cycle_edges,
            key=lambda e: (KIND_RANK[e["kind"]], e["weight"], e["from"], e["to"]),
        )
        del remaining[(victim["from"], victim["to"])]
        removed.append(victim)
    state["removed_edges"] = removed
    state["remaining_edges"] = remaining
    state["converged"] = converged

    # stage 12 install order -------------------------------------------------
    indegree = {oid: 0 for oid in ids_sorted}
    successors = {oid: [] for oid in ids_sorted}
    for (src, dst) in sorted(remaining):
        successors[src].append(dst)
        indegree[dst] += 1
    ready = [oid for oid in ids_sorted if indegree[oid] == 0]
    order = []
    while ready:
        ready.sort(key=lambda oid: (setup_seconds[oid], oid))
        nxt = ready.pop(0)
        order.append(nxt)
        for dst in successors[nxt]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)
    placed_in_order = set(order)
    for oid in ids_sorted:
        if oid not in placed_in_order:
            order.append(oid)
    state["install_order"] = order
    state["install_position"] = {oid: i + 1 for i, oid in enumerate(order)}

    # stage 8 shared crate capacity -----------------------------------------
    ledger = {}
    for cls in sorted(data["crates"]):
        catalog = data["crates"][cls]["count"]
        consumed = data["prior"].get(cls, 0)
        ledger[cls] = {
            "crate_class": cls,
            "catalog_count": catalog,
            "prior_consumed": consumed,
            "remaining": catalog - consumed,
        }
    reservations = dict(data["reservations"])
    assignment = {}
    for oid in order:
        obj = by_id[oid]
        options = []
        for cls, residual in crate_candidates[oid]:
            if ledger[cls]["remaining"] <= 0:
                continue
            held = reservations.get((cls, obj["lender_id"]), 0) > 0
            options.append(((0 if held else 1, residual, cls), cls, residual, held))
        if not options:
            assignment[oid] = {
                "crate_class": "",
                "code": "C_NO_CRATE",
                "primary_class": primary_crate[oid],
                "residual": 0,
            }
            continue
        options.sort(key=lambda item: item[0])
        _, cls, residual, held = options[0]
        ledger[cls]["remaining"] -= 1
        if held:
            reservations[(cls, obj["lender_id"])] -= 1
        assignment[oid] = {
            "crate_class": cls,
            "code": "C_PRIMARY" if cls == primary_crate[oid] else "C_DISPLACED",
            "primary_class": primary_crate[oid],
            "residual": residual,
        }
    state["ledger"] = ledger
    state["assignment"] = assignment

    # stage 9 transport legs -------------------------------------------------
    legs = []
    for obj in objects:
        oid = obj["object_id"]
        cls = assignment[oid]["crate_class"]
        if not cls:
            continue
        mode = data["crates"][cls]["transport_mode"]
        exception = False
        rule = data["transport"].get(obj["lender_id"])
        if rule and rule[0] == mode:
            mode = rule[1]
            exception = True
        legs.append(
            {
                "object_id": oid,
                "crate_class": cls,
                "mode": mode,
                "exception": exception,
            }
        )
    state["legs"] = legs

    # stage 13 windows -------------------------------------------------------
    windows = {}
    for obj in objects:
        oid = obj["object_id"]
        required = ceil_div(setup_seconds[oid], SECONDS_PER_DAY)
        bay_id = adjusted[oid]["bay_id"]
        if not bay_id:
            windows[oid] = {
                "object_id": oid,
                "bay_id": "",
                "window_start": -1,
                "window_end": -1,
                "required_days": required,
                "code": "S_NO_PLACEMENT",
            }
            continue
        cons = data["conservation"].get(obj["conservation_class"], {})
        access_start, access_end = data["venue"].get(bay_id, (0, 0))
        crew_start, crew_end = data["crew"].get(obj["crew_class"], (0, 0))
        low = max(cons.get("setup_start", 0), access_start, crew_start)
        high = min(cons.get("setup_end", 0), access_end - 1, crew_end)
        if not assignment[oid]["crate_class"]:
            code = "S_NO_CRATE"
        elif high < low:
            code = "S_NO_WINDOW"
        elif required > high - low + 1:
            code = "S_SHORT_WINDOW"
        else:
            code = "S_OK"
        windows[oid] = {
            "object_id": oid,
            "bay_id": bay_id,
            "window_start": low,
            "window_end": high,
            "required_days": required,
            "code": code,
        }
    state["windows"] = windows

    # stage 14 dossier figures ----------------------------------------------
    unschedulable = []
    displaced = []
    unschedulable_penalty = 0
    contention_penalty = 0
    total_insurance = 0
    total_residual = 0
    insurance = {}
    for oid in ids_sorted:
        obj = by_id[oid]
        entry = value_class_of(obj, data)
        if windows[oid]["code"] != "S_OK":
            unschedulable.append((oid, windows[oid]["code"]))
            if entry:
                unschedulable_penalty += entry["unschedulable_penalty"]
        if assignment[oid]["code"] == "C_DISPLACED":
            displaced.append(oid)
            if entry:
                contention_penalty += entry["contention_penalty"]
        lender = data["lenders"].get(obj["lender_id"], {})
        insurance[oid] = round_half_up(
            obj["declared_value_cents"] * lender.get("insurance_rate_num", 0),
            lender.get("insurance_rate_den", 0),
        )
        total_insurance += insurance[oid]
        total_residual += assignment[oid]["residual"]

    state["insurance"] = insurance
    state["unschedulable"] = sorted(unschedulable)
    state["displaced"] = displaced
    state["unschedulable_penalty"] = unschedulable_penalty
    state["contention_penalty"] = contention_penalty
    state["total_insurance"] = total_insurance
    state["total_residual"] = total_residual
    state["total_setup_seconds"] = sum(setup_seconds[oid] for oid in ids_sorted)
    state["placed_objects"] = sum(
        1 for oid in ids_sorted if windows[oid]["code"] == "S_OK"
    )
    state["by_bay"] = by_bay
    state["bays_by_id"] = bays_by_id
    return state


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------

def emit(data, state, out_dir):
    objects = state["objects"]
    ids_sorted = sorted(state["by_id"])
    adjusted = state["adjusted"]
    selected = state["selected"]
    assignment = state["assignment"]
    windows = state["windows"]

    write_jsonl(
        out_dir,
        "canonical_objects.jsonl",
        [
            {
                "object_id": obj["object_id"],
                "lender_id": obj["lender_id"],
                "material_code": obj["material_code"],
                "version": obj["version"],
                "declared_value_cents": obj["declared_value_cents"],
                "mass_g": obj["mass_g"],
            }
            for obj in objects
        ],
    )

    write_jsonl(
        out_dir,
        "normalized_geometry.jsonl",
        [
            {
                "object_id": obj["object_id"],
                "clearance_w": obj["clearance_w"],
                "clearance_h": obj["clearance_h"],
                "clearance_d": obj["clearance_d"],
                "aspect_num": obj["aspect_num"],
                "aspect_den": obj["aspect_den"],
            }
            for obj in objects
        ],
    )

    write_jsonl(
        out_dir,
        "admissibility_matrix.jsonl",
        [
            {
                "object_id": obj["object_id"],
                "orientations": obj["orientations"],
                "tip_allowed": obj["tip_allowed"],
                "rotate_allowed": obj["rotate_allowed"],
            }
            for obj in objects
        ],
    )

    write_jsonl(out_dir, "bay_candidate_matrix.jsonl", state["bay_candidates"])

    write_jsonl(
        out_dir,
        "selected_placement.jsonl",
        [
            dict(selected[obj["object_id"]], object_id=obj["object_id"])
            for obj in objects
        ],
    )

    write_jsonl(
        out_dir,
        "rejected_placement.jsonl",
        [{"object_id": oid, "code": code} for oid, code in state["rejected"]],
    )

    write_jsonl(
        out_dir,
        "conservation_adjusted.jsonl",
        [
            dict(adjusted[obj["object_id"]], object_id=obj["object_id"])
            for obj in objects
        ],
    )

    crate_rows = []
    for oid in ids_sorted:
        for cls, residual in state["crate_candidates"][oid]:
            crate_rows.append(
                {"object_id": oid, "crate_class": cls, "residual": residual}
            )
    write_jsonl(out_dir, "crate_candidate_matrix.jsonl", crate_rows)

    write_jsonl(
        out_dir,
        "crate_assignment.jsonl",
        [
            {
                "object_id": oid,
                "crate_class": assignment[oid]["crate_class"],
                "code": assignment[oid]["code"],
                "primary_class": assignment[oid]["primary_class"],
            }
            for oid in ids_sorted
        ],
    )

    write_jsonl(
        out_dir,
        "crate_capacity_ledger.jsonl",
        [state["ledger"][cls] for cls in sorted(state["ledger"])],
    )

    write_jsonl(
        out_dir,
        "crate_contention_report.jsonl",
        [
            {
                "object_id": oid,
                "wanted_class": assignment[oid]["primary_class"],
                "assigned_class": assignment[oid]["crate_class"],
            }
            for oid in ids_sorted
            if assignment[oid]["code"] == "C_DISPLACED"
        ],
    )

    write_jsonl(out_dir, "transport_leg_manifest.jsonl", state["legs"])

    write_jsonl(
        out_dir,
        "dependency_edges.jsonl",
        [state["edges"][key] for key in sorted(state["edges"])],
    )

    write_jsonl(out_dir, "cycle_resolution_audit.jsonl", state["removed_edges"])

    write_jsonl(
        out_dir,
        "install_order.jsonl",
        [
            {"position": index + 1, "object_id": oid}
            for index, oid in enumerate(state["install_order"])
        ],
    )

    write_jsonl(
        out_dir,
        "crew_requirement_ledger.jsonl",
        [
            {
                "object_id": obj["object_id"],
                "crew_class": obj["crew_class"],
                "setup_seconds": state["setup_seconds"][obj["object_id"]],
            }
            for obj in objects
        ],
    )

    write_jsonl(
        out_dir,
        "install_window_table.jsonl",
        [windows[obj["object_id"]] for obj in objects],
    )

    write_jsonl(
        out_dir,
        "unschedulable_report.jsonl",
        [{"object_id": oid, "code": code} for oid, code in state["unschedulable"]],
    )

    write_jsonl(
        out_dir,
        "gallery_occupancy_map.jsonl",
        [
            {
                "bay_id": bay["bay_id"],
                "object_count": len(state["by_bay"].get(bay["bay_id"], [])),
                "objects": sorted(state["by_bay"].get(bay["bay_id"], [])),
            }
            for bay in state["bays_by_id"]
        ],
    )

    write_jsonl(
        out_dir,
        "deterministic_audit.jsonl",
        [
            {
                "object_id": obj["object_id"],
                "placed_bay": adjusted[obj["object_id"]]["bay_id"],
                "crate_class": assignment[obj["object_id"]]["crate_class"],
                "displaced": assignment[obj["object_id"]]["code"] == "C_DISPLACED",
                "scheduled": windows[obj["object_id"]]["code"] == "S_OK",
                "install_position": state["install_position"][obj["object_id"]],
                "setup_seconds": state["setup_seconds"][obj["object_id"]],
                "insurance_cents": state["insurance"][obj["object_id"]],
            }
            for obj in objects
        ],
    )

    write_json(
        out_dir,
        "penalty_summary.json",
        {
            "unschedulable_penalty": state["unschedulable_penalty"],
            "contention_penalty": state["contention_penalty"],
            "cycles_broken": len(state["removed_edges"]),
            "total_insurance_cents": state["total_insurance"],
        },
    )

    objective = [
        state["placed_objects"],
        state["unschedulable_penalty"],
        state["contention_penalty"],
        len(state["removed_edges"]),
        state["total_residual"],
    ]
    write_json(
        out_dir,
        "objective_tuple.json",
        {"objective": objective, "order": list(data["objective_order"])},
    )

    program_path = os.path.abspath(__file__)
    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json"]:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest[os.path.basename(program_path)] = sha256_file(program_path)
    write_json(out_dir, "integrity_manifest.json", manifest)

    chain = data["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(
        out_dir,
        "final_state.json",
        {
            "chain": chain,
            "repair_converged": state["converged"],
            "cycle_edges_removed": len(state["removed_edges"]),
            "final_cycle_count": 0 if state["converged"] else 1,
            "displacement_count": len(state["displaced"]),
            "unschedulable_count": len(state["unschedulable"]),
            "placed_objects": state["placed_objects"],
            "total_setup_seconds": state["total_setup_seconds"],
            "total_insurance_cents": state["total_insurance"],
            "objective": objective,
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    data = load(data_dir)
    emit(data, derive(data), out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
