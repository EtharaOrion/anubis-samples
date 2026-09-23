#!/usr/bin/env python3
"""Derive the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Implements the staged derivation written in rules.md / schema.md /
constants.md / reason_codes.md that ship inside the data directory.  All
arithmetic is integer or exact rational; no float is produced, compared or
written anywhere.
"""

import csv
import hashlib
import itertools
import json
import math
import os
import sys

LEN_FACTORS = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTORS = {"G": 1, "KG": 1000}
SECONDS_PER_DAY = 3600
DEFAULT_SETUP_DIV = 700
DEFAULT_CHAIN_SEED = "0" * 64

# Ascending removal precedence: a bay size edge goes first, an explicit rule last.
KIND_RANK = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}


# ---------------------------------------------------------------- utilities


def round_half_up(num, den):
    """Round num/den half up; a zero denominator yields the zero sentinel."""
    if den == 0:
        return 0
    quot, rem = divmod(num, den)
    if 2 * rem >= den:
        quot += 1
    return quot


def reduce_ratio(num, den):
    """Reduce to lowest terms; a zero denominator yields the 0/1 sentinel."""
    if den == 0:
        return 0, 1
    gcd = math.gcd(num, den)
    if gcd:
        num //= gcd
        den //= gcd
    return num, den


def to_int(text, default=0):
    text = (text or "").strip()
    if text == "":
        return default
    return int(text)


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle)]


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_bytes(path, payload):
    with open(path, "wb") as handle:
        handle.write(payload)


def jsonl_bytes(rows):
    parts = []
    for row in rows:
        parts.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        parts.append("\n")
    return "".join(parts).encode("utf-8")


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_hex(payload):
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- stage 1


def canonicalize(data_dir):
    horizon = load_json(os.path.join(data_dir, "horizon_config.json"))
    setup_div = int(horizon.get("setup_div", DEFAULT_SETUP_DIV))
    chain_seed = str(horizon.get("chain_seed", DEFAULT_CHAIN_SEED))

    order_ids = []
    master = {}
    for row in load_csv(os.path.join(data_dir, "object_master.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid or oid in master:  # stale duplicate rows lose to the first
            continue
        master[oid] = {
            "lender_id": (row.get("lender_id") or "").strip(),
            "material_code": (row.get("material_code") or "").strip(),
        }
        order_ids.append(oid)

    versions = {}
    for row in load_csv(os.path.join(data_dir, "object_version_map.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        version = to_int(row.get("version"), 0)
        versions[oid] = max(versions.get(oid, version), version)

    dims = {}
    for row in load_csv(os.path.join(data_dir, "object_dimensions.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        factor = LEN_FACTORS[(row.get("dim_unit") or "MM").strip()]
        dims[oid] = (
            to_int(row.get("width")) * factor,
            to_int(row.get("height")) * factor,
            to_int(row.get("depth")) * factor,
        )

    masses = {}
    for row in load_csv(os.path.join(data_dir, "object_mass.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        factor = MASS_FACTORS[(row.get("mass_unit") or "G").strip()]
        masses[oid] = to_int(row.get("mass")) * factor

    values = {}
    for row in load_csv(os.path.join(data_dir, "object_value.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        values[oid] = to_int(row.get("declared_value")) * 100

    turns = {}
    for row in load_csv(os.path.join(data_dir, "object_orientation.csv")):
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        turns[oid] = (to_int(row.get("tip_allowed")), to_int(row.get("rotate_allowed")))

    objects = []
    for oid in order_ids:
        width, height, depth = dims.get(oid, (0, 0, 0))
        tip, rotate = turns.get(oid, (0, 0))
        objects.append(
            {
                "object_id": oid,
                "lender_id": master[oid]["lender_id"],
                "material_code": master[oid]["material_code"],
                "version": versions.get(oid, 0),
                "declared_value_cents": values.get(oid, 0),
                "mass_g": masses.get(oid, 0),
                "width": width,
                "height": height,
                "depth": depth,
                "tip_allowed": tip,
                "rotate_allowed": rotate,
            }
        )
    return objects, setup_div, chain_seed


def load_reference(data_dir):
    ref = {}

    materials = {}
    for row in load_csv(os.path.join(data_dir, "material_rules.csv")):
        code = (row.get("material_code") or "").strip()
        if not code or code in materials:
            continue
        materials[code] = {
            "conservation_class": (row.get("conservation_class") or "").strip(),
            "max_lux": to_int(row.get("max_lux")),
            "handling_class": (row.get("handling_class") or "").strip(),
        }
    ref["material"] = materials

    handling = {}
    for row in load_csv(os.path.join(data_dir, "handling_class_rules.csv")):
        code = (row.get("handling_class") or "").strip()
        if not code or code in handling:
            continue
        handling[code] = {
            "clearance_w": to_int(row.get("clearance_w")) * 1000,
            "clearance_d": to_int(row.get("clearance_d")) * 1000,
            "clearance_h": to_int(row.get("clearance_h")) * 1000,
            "handle_margin": to_int(row.get("handle_margin")) * 1000,
            "crew_class": (row.get("crew_class") or "").strip(),
            "setup_base": to_int(row.get("setup_base")),
        }
    ref["handling"] = handling

    bays = {}
    for row in load_csv(os.path.join(data_dir, "gallery_bays.csv")):
        bay_id = (row.get("bay_id") or "").strip()
        if not bay_id or bay_id in bays:
            continue
        bays[bay_id] = {
            "interior_w": to_int(row.get("interior_w")) * 1000,
            "interior_h": to_int(row.get("interior_h")) * 1000,
            "interior_d": to_int(row.get("interior_d")) * 1000,
            "lux_level": to_int(row.get("lux_level")),
            "bay_priority": to_int(row.get("bay_priority")),
        }
    ref["bays"] = bays

    blocked = {}
    for row in load_csv(os.path.join(data_dir, "bay_exceptions.csv")):
        bay = (row.get("bay_id") or "").strip()
        cls = (row.get("blocked_class") or "").strip()
        if bay and cls:
            blocked.setdefault(bay, set()).add(cls)
    ref["blocked"] = blocked

    crates = {}
    for row in load_csv(os.path.join(data_dir, "crate_catalog.csv")):
        cls = (row.get("crate_class") or "").strip()
        if not cls or cls in crates:
            continue
        crates[cls] = {
            "inner": sorted(
                (
                    to_int(row.get("inner_w")) * 1000,
                    to_int(row.get("inner_h")) * 1000,
                    to_int(row.get("inner_d")) * 1000,
                )
            ),
            "count": to_int(row.get("count")),
            "transport_mode": (row.get("transport_mode") or "").strip(),
        }
    ref["crates"] = crates

    reservations = {}
    for row in load_csv(os.path.join(data_dir, "crate_reservations.csv")):
        key = (
            (row.get("crate_class") or "").strip(),
            (row.get("lender_id") or "").strip(),
        )
        if all(key):
            reservations[key] = (
                reservations.get(key, 0) + to_int(row.get("reserved_count"))
            )
    ref["reservations"] = reservations

    prior = {}
    for row in load_csv(os.path.join(data_dir, "prior_state.csv")):
        cls = (row.get("crate_class") or "").strip()
        if cls:
            prior[cls] = prior.get(cls, 0) + to_int(row.get("consumed_count"))
    ref["prior"] = prior

    conservation = {}
    for row in load_csv(os.path.join(data_dir, "conservation_rules.csv")):
        code = (row.get("conservation_class") or "").strip()
        if not code or code in conservation:
            continue
        conservation[code] = {
            "setup_start": to_int(row.get("setup_start")),
            "setup_end": to_int(row.get("setup_end")),
            "needs_dark": to_int(row.get("needs_dark")),
        }
    ref["conservation"] = conservation

    venue = {}
    for row in load_csv(os.path.join(data_dir, "venue_windows.csv")):
        bay_id = (row.get("bay_id") or "").strip()
        if not bay_id or bay_id in venue:
            continue
        venue[bay_id] = {
            "access_start": to_int(row.get("access_start")),
            "access_end": to_int(row.get("access_end")),
        }
    ref["venue"] = venue

    crew = {}
    for row in load_csv(os.path.join(data_dir, "crew_roster.csv")):
        code = (row.get("crew_class") or "").strip()
        if not code or code in crew:
            continue
        crew[code] = {
            "avail_start": to_int(row.get("avail_start")),
            "avail_end": to_int(row.get("avail_end")),
        }
    ref["crew"] = crew

    transport = {}
    for row in load_csv(os.path.join(data_dir, "transport_rules.csv")):
        code = (row.get("lender_id") or "").strip()
        if not code or code in transport:
            continue
        transport[code] = {
            "forbidden_mode": (row.get("forbidden_mode") or "").strip(),
            "fallback_mode": (row.get("fallback_mode") or "").strip(),
        }
    ref["transport"] = transport

    lenders = {}
    for row in load_csv(os.path.join(data_dir, "lender_master.csv")):
        code = (row.get("lender_id") or "").strip()
        if not code or code in lenders:
            continue
        lenders[code] = {
            "insurance_rate_num": to_int(row.get("insurance_rate_num")),
            "insurance_rate_den": to_int(row.get("insurance_rate_den")),
            "pref_mode": (row.get("pref_mode") or "").strip(),
        }
    ref["lender"] = lenders

    ref["value_class"] = sorted(
        (
            {
                "value_class": (row.get("value_class") or "").strip(),
                "min_value": to_int(row.get("min_value")),
                "unschedulable_penalty": to_int(row.get("unschedulable_penalty")),
                "contention_penalty": to_int(row.get("contention_penalty")),
            }
            for row in load_csv(os.path.join(data_dir, "value_class_rules.csv"))
        ),
        key=lambda r: (-r["min_value"], r["value_class"]),
    )

    ref["dep_rules"] = [
        {
            "from": (row.get("from_object") or "").strip(),
            "to": (row.get("to_object") or "").strip(),
            "edge_priority": to_int(row.get("edge_priority")),
        }
        for row in load_csv(os.path.join(data_dir, "dependency_rules.csv"))
    ]

    return ref


# ---------------------------------------------------------------- stages 2-6


NO_MATERIAL = {
    "conservation_class": "",
    "max_lux": 10 ** 18,
    "handling_class": "",
}
NO_HANDLING = {
    "clearance_w": 0,
    "clearance_d": 0,
    "clearance_h": 0,
    "handle_margin": 0,
    "crew_class": "",
    "setup_base": 0,
}
UNBOUNDED = {"start": -(10 ** 18), "end": 10 ** 18}


def normalize(objects, ref):
    for obj in objects:
        material = ref["material"].get(obj["material_code"], NO_MATERIAL)
        handling = ref["handling"].get(material["handling_class"], NO_HANDLING)
        obj["conservation_class"] = material["conservation_class"]
        obj["max_lux"] = material["max_lux"]
        obj["handling"] = handling

        obj["clearance_w"] = obj["width"] + 2 * handling["clearance_w"]
        obj["clearance_d"] = obj["depth"] + 2 * handling["clearance_d"]
        obj["clearance_h"] = obj["height"] + handling["clearance_h"]
        obj["aspect_num"], obj["aspect_den"] = reduce_ratio(
            obj["height"], obj["width"]
        )

        orientations = [0]
        if obj["rotate_allowed"]:
            orientations.append(1)
        if obj["tip_allowed"]:
            orientations.extend([2, 3])
        obj["orientations"] = orientations

        margin = handling["handle_margin"]
        obj["handling_dims"] = (
            obj["width"] + 2 * margin,
            obj["height"] + 2 * margin,
            obj["depth"] + 2 * margin,
        )
        obj["handling_volume"] = (
            obj["handling_dims"][0] * obj["handling_dims"][1] * obj["handling_dims"][2]
        )


def oriented(obj, index):
    """Return (footprint width, footprint depth, vertical extent)."""
    cw, ch, cd = obj["clearance_w"], obj["clearance_h"], obj["clearance_d"]
    if index == 0:
        return cw, cd, ch
    if index == 1:
        return cd, cw, ch
    if index == 2:
        return cw, ch, cd
    return ch, cw, cd


def orientation_fits(obj, index, bay):
    fw, fd, vert = oriented(obj, index)
    return fw <= bay["interior_w"] and fd < bay["interior_d"] and vert <= bay["interior_h"]


def smallest_fitting(obj, bay):
    for index in obj["orientations"]:
        if orientation_fits(obj, index, bay):
            return index
    return None


def passes_conservation(obj, bay_id, ref):
    bay = ref["bays"][bay_id]
    if bay["lux_level"] > obj["max_lux"]:
        return False
    if obj["conservation_class"] and obj["conservation_class"] in ref["blocked"].get(
        bay_id, ()
    ):
        return False
    rules = ref["conservation"].get(obj["conservation_class"])
    if rules and rules["needs_dark"] and bay["lux_level"] > 0:
        return False
    return True


def place(objects, ref):
    scan = sorted(ref["bays"], key=lambda b: (ref["bays"][b]["bay_priority"], b))
    for obj in objects:
        primary_bay, primary_orient = None, None
        for bay_id in scan:
            index = smallest_fitting(obj, ref["bays"][bay_id])
            if index is not None:
                primary_bay, primary_orient = bay_id, index
                break
        obj["primary_bay"] = primary_bay
        obj["primary_orientation"] = primary_orient
        obj["placement_code"] = "P_OK" if primary_bay is not None else (
            "P_NO_ORIENTATION" if obj["orientations"] == [0] else "P_NO_FIT"
        )
        if primary_bay is None:
            obj["bay_id"] = None
            obj["orientation"] = None
            obj["conservation_code"] = obj["placement_code"]
            continue

        cons_bay, cons_orient = None, None
        for bay_id in scan:
            index = smallest_fitting(obj, ref["bays"][bay_id])
            if index is None:
                continue
            if passes_conservation(obj, bay_id, ref):
                cons_bay, cons_orient = bay_id, index
                break
        obj["bay_id"] = cons_bay
        obj["orientation"] = cons_orient
        obj["conservation_code"] = (
            "P_OK" if cons_bay is not None else "P_CONSERVATION_BLOCK"
        )


# ---------------------------------------------------------------- stage 7


def crate_candidates(objects, ref):
    for obj in objects:
        dims = sorted(obj["handling_dims"])
        volume = obj["handling_volume"]
        fits = {}
        for cls, crate in ref["crates"].items():
            inner = crate["inner"]
            if all(dims[i] <= inner[i] for i in range(3)):
                fits[cls] = inner[0] * inner[1] * inner[2] - volume
        obj["crate_fits"] = fits
        obj["primary_crate"] = (
            min(fits, key=lambda cls: (fits[cls], cls)) if fits else ""
        )


# ---------------------------------------------------------------- stages 10-12


def build_graph(objects, ref, order_ids):
    by_id = {obj["object_id"]: obj for obj in objects}
    edges = {}

    def add(src, dst, kind, weight):
        key = (src, dst)
        if key in edges and KIND_RANK[edges[key]["kind"]] >= KIND_RANK[kind]:
            return  # the higher precedence kind already occupies this pair
        edges[key] = {"from": src, "to": dst, "kind": kind, "weight": weight}

    for rule in ref["dep_rules"]:
        if rule["from"] in by_id and rule["to"] in by_id:
            add(rule["from"], rule["to"], "D_RULE", rule["edge_priority"])

    bays = {}
    for oid in order_ids:
        bay = by_id[oid]["bay_id"]
        if bay:
            bays.setdefault(bay, []).append(oid)
    for members in bays.values():
        for left, right in itertools.combinations(sorted(members), 2):
            a, b = by_id[left], by_id[right]
            # bay size: larger handling volume installs first, ties by lower id
            if a["handling_volume"] >= b["handling_volume"]:
                add(left, right, "D_BAYSIZE", a["handling_volume"])
            else:
                add(right, left, "D_BAYSIZE", b["handling_volume"])
            # conservation: the more sensitive class installs first
            if a["conservation_class"] != b["conservation_class"]:
                first, second = (
                    (a, b)
                    if a["conservation_class"] > b["conservation_class"]
                    else (b, a)
                )
                add(
                    first["object_id"],
                    second["object_id"],
                    "D_CONSERVATION",
                    first["handling_volume"],
                )
    return edges


def find_cycle(nodes, adjacency):
    """Deterministic DFS: nodes and successors visited in ascending order."""
    state = {}
    for root in nodes:
        if state.get(root):
            continue
        state[root] = 1
        path = [root]
        stack = [(root, iter(adjacency.get(root, ())))]
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if state.get(child) is None:
                    state[child] = 1
                    path.append(child)
                    stack.append((child, iter(adjacency.get(child, ()))))
                    advanced = True
                    break
                if state.get(child) == 1:
                    return path[path.index(child):] + [child]
            if not advanced:
                state[node] = 2
                stack.pop()
                path.pop()
    return None


def resolve_cycles(edges, nodes):
    """Remove the lowest priority edge of each cycle until acyclic.

    Returns (removed edges in removal order, cycles left, whether it converged).
    """
    removed = []
    while True:
        adjacency = {}
        for edge in edges.values():
            adjacency.setdefault(edge["from"], []).append(edge["to"])
        for key in adjacency:
            adjacency[key].sort()
        cycle = find_cycle(nodes, adjacency)
        if cycle is None:
            return removed, 0, True
        pairs = set(zip(cycle, cycle[1:]))
        candidates = [e for e in edges.values() if (e["from"], e["to"]) in pairs]
        if not candidates:  # cannot happen for a real cycle; guards the loop
            return removed, 1, False
        victim = min(
            candidates,
            key=lambda e: (KIND_RANK[e["kind"]], e["weight"], e["from"], e["to"]),
        )
        del edges[(victim["from"], victim["to"])]
        removed.append(dict(victim))


def install_order(objects, edges, order_ids):
    by_id = {obj["object_id"]: obj for obj in objects}
    installed = set()
    predecessors = {oid: set() for oid in order_ids}
    for edge in edges.values():
        predecessors.setdefault(edge["to"], set()).add(edge["from"])
    remaining = sorted(order_ids)
    ordered = []
    position = 1
    while remaining:
        ready = [oid for oid in remaining if predecessors[oid] <= installed]
        if not ready:  # unreachable once the graph is acyclic
            ready = [remaining[0]]
        choice = min(ready, key=lambda oid: (by_id[oid]["setup_seconds"], oid))
        by_id[choice]["install_position"] = position
        ordered.append(choice)
        installed.add(choice)
        remaining.remove(choice)
        position += 1
    return ordered


# ---------------------------------------------------------------- stage 8


def assign_crates(objects, ref, ordered):
    by_id = {obj["object_id"]: obj for obj in objects}
    remaining = {
        cls: crate["count"] - ref["prior"].get(cls, 0)
        for cls, crate in ref["crates"].items()
    }
    reservations = dict(ref["reservations"])
    for oid in ordered:
        obj = by_id[oid]
        available = [
            cls for cls in obj["crate_fits"] if remaining.get(cls, 0) > 0
        ]
        if not available:
            obj["crate_class"] = ""
            obj["crate_code"] = "C_NO_CRATE"
            continue
        choice = min(
            available,
            key=lambda cls: (
                0 if reservations.get((cls, obj["lender_id"]), 0) > 0 else 1,
                obj["crate_fits"][cls],
                cls,
            ),
        )
        remaining[choice] -= 1
        if reservations.get((choice, obj["lender_id"]), 0) > 0:
            reservations[(choice, obj["lender_id"])] -= 1
        obj["crate_class"] = choice
        obj["crate_code"] = (
            "C_PRIMARY" if choice == obj["primary_crate"] else "C_DISPLACED"
        )
    return remaining


# ---------------------------------------------------------------- stage 13


def compute_setup(objects, ref, setup_div):
    """Stage 13 setup half: crew class, rounded setup seconds, required days."""
    for obj in objects:
        handling = obj["handling"]
        obj["crew_class"] = handling["crew_class"]
        obj["setup_seconds"] = round_half_up(
            handling["setup_base"] * obj["mass_g"], setup_div
        )
        obj["required_days"] = -(-obj["setup_seconds"] // SECONDS_PER_DAY)


def compute_windows(objects, ref):
    """Stage 13 window half; runs after crate assignment, which can gate it."""
    for obj in objects:
        if not obj["bay_id"]:
            obj["window_start"] = 0
            obj["window_end"] = 0
            obj["window_code"] = "S_NO_PLACEMENT"
        elif obj.get("crate_code") == "C_NO_CRATE":
            obj["window_start"] = 0
            obj["window_end"] = 0
            obj["window_code"] = "S_NO_CRATE"
        else:
            cons = ref["conservation"].get(obj["conservation_class"], {
                "setup_start": UNBOUNDED["start"], "setup_end": UNBOUNDED["end"],
            })
            crew = ref["crew"].get(obj["crew_class"], {
                "avail_start": UNBOUNDED["start"], "avail_end": UNBOUNDED["end"],
            })
            lows = [cons["setup_start"], crew["avail_start"]]
            highs = [cons["setup_end"], crew["avail_end"]]
            venue = ref["venue"].get(obj["bay_id"])
            if venue is not None:
                lows.append(venue["access_start"])
                highs.append(venue["access_end"] - 1)  # venue end is exclusive
            low, high = max(lows), min(highs)
            days = high - low + 1
            if days <= 0:
                obj["window_code"] = "S_NO_WINDOW"
            elif obj["required_days"] > days:
                obj["window_code"] = "S_SHORT_WINDOW"
            else:
                obj["window_code"] = "S_OK"
            obj["window_start"] = low
            obj["window_end"] = high


def schedule(objects, ref, setup_div):
    compute_setup(objects, ref, setup_div)
    compute_windows(objects, ref)


# ---------------------------------------------------------------- stage 14


def value_class_of(obj, ref):
    declared = obj["declared_value_cents"]
    for entry in ref["value_class"]:
        if declared >= entry["min_value"] * 100:
            return entry
    return ref["value_class"][-1]


def derive(data_dir, out_dir):
    objects, setup_div, chain_seed = canonicalize(data_dir)
    ref = load_reference(data_dir)
    order_ids = [obj["object_id"] for obj in objects]
    by_id = {obj["object_id"]: obj for obj in objects}

    normalize(objects, ref)
    place(objects, ref)
    crate_candidates(objects, ref)
    edges = build_graph(objects, ref, order_ids)
    edge_rows = sorted(
        (dict(e) for e in edges.values()), key=lambda e: (e["from"], e["to"])
    )
    removed, cycles_left, converged = resolve_cycles(edges, sorted(order_ids))
    compute_setup(objects, ref, setup_div)
    ordered = install_order(objects, edges, order_ids)
    remaining = assign_crates(objects, ref, ordered)
    compute_windows(objects, ref)

    canonical_rows = [
        {
            "object_id": o["object_id"],
            "lender_id": o["lender_id"],
            "material_code": o["material_code"],
            "version": o["version"],
            "declared_value_cents": o["declared_value_cents"],
            "mass_g": o["mass_g"],
        }
        for o in objects
    ]
    geometry_rows = [
        {
            "object_id": o["object_id"],
            "clearance_w": o["clearance_w"],
            "clearance_h": o["clearance_h"],
            "clearance_d": o["clearance_d"],
            "aspect_num": o["aspect_num"],
            "aspect_den": o["aspect_den"],
        }
        for o in objects
    ]
    admissibility_rows = [
        {
            "object_id": o["object_id"],
            "orientations": o["orientations"],
            "tip_allowed": o["tip_allowed"],
            "rotate_allowed": o["rotate_allowed"],
        }
        for o in objects
    ]
    bay_candidate_rows = []
    for obj in objects:
        for bay_id in sorted(ref["bays"]):
            index = smallest_fitting(obj, ref["bays"][bay_id])
            bay_candidate_rows.append(
                {
                    "object_id": obj["object_id"],
                    "bay_id": bay_id,
                    "fits": index is not None,
                    "orientation": -1 if index is None else index,
                }
            )
    selected_rows = [
        {
            "object_id": o["object_id"],
            "bay_id": o["primary_bay"] or "",
            "orientation": -1 if o["primary_orientation"] is None else o["primary_orientation"],
            "code": o["placement_code"],
        }
        for o in objects
    ]
    conservation_rows = [
        {
            "object_id": o["object_id"],
            "bay_id": o["bay_id"] or "",
            "orientation": -1 if o["orientation"] is None else o["orientation"],
            "code": o["conservation_code"],
        }
        for o in objects
    ]
    rejected_rows = sorted(
        (
            {"object_id": o["object_id"], "code": o["conservation_code"]}
            for o in objects
            if o["conservation_code"] != "P_OK"
        ),
        key=lambda r: (r["object_id"], r["code"]),
    )
    crate_candidate_rows = []
    for oid in sorted(order_ids):
        fits = by_id[oid]["crate_fits"]
        for cls in sorted(fits, key=lambda c: (fits[c], c)):
            crate_candidate_rows.append(
                {"object_id": oid, "crate_class": cls, "residual": fits[cls]}
            )
    assignment_rows = [
        {
            "object_id": o["object_id"],
            "crate_class": o["crate_class"],
            "code": o["crate_code"],
            "primary_class": o["primary_crate"],
        }
        for o in sorted(objects, key=lambda x: x["object_id"])
    ]
    ledger_rows = [
        {
            "crate_class": cls,
            "catalog_count": ref["crates"][cls]["count"],
            "prior_consumed": ref["prior"].get(cls, 0),
            "remaining": remaining[cls],
        }
        for cls in sorted(ref["crates"])
    ]
    contention_rows = [
        {
            "object_id": o["object_id"],
            "wanted_class": o["primary_crate"],
            "assigned_class": o["crate_class"],
        }
        for o in sorted(objects, key=lambda x: x["object_id"])
        if o["crate_code"] == "C_DISPLACED"
    ]
    transport_rows = []
    for obj in objects:
        if not obj["crate_class"]:
            continue
        mode = ref["crates"][obj["crate_class"]]["transport_mode"]
        rule = ref["transport"].get(obj["lender_id"])
        exception = bool(rule and rule["forbidden_mode"] == mode)
        if exception:
            mode = rule["fallback_mode"]
        transport_rows.append(
            {
                "object_id": obj["object_id"],
                "crate_class": obj["crate_class"],
                "mode": mode,
                "exception": exception,
            }
        )
    install_rows = sorted(
        ({"position": by_id[oid]["install_position"], "object_id": oid} for oid in ordered),
        key=lambda r: r["position"],
    )
    crew_rows = [
        {
            "object_id": o["object_id"],
            "crew_class": o["crew_class"],
            "setup_seconds": o["setup_seconds"],
        }
        for o in objects
    ]
    window_rows = [
        {
            "object_id": o["object_id"],
            "bay_id": o["bay_id"] or "",
            "window_start": o["window_start"],
            "window_end": o["window_end"],
            "required_days": o["required_days"],
            "code": o["window_code"],
        }
        for o in objects
    ]
    unschedulable_rows = sorted(
        (
            {"object_id": o["object_id"], "code": o["window_code"]}
            for o in objects
            if o["window_code"] != "S_OK"
        ),
        key=lambda r: (r["object_id"], r["code"]),
    )
    occupancy = {bay_id: [] for bay_id in ref["bays"]}
    for obj in objects:
        if obj["bay_id"]:
            occupancy.setdefault(obj["bay_id"], []).append(obj["object_id"])
    occupancy_rows = [
        {
            "bay_id": bay_id,
            "object_count": len(occupancy[bay_id]),
            "objects": sorted(occupancy[bay_id]),
        }
        for bay_id in sorted(occupancy)
    ]

    total_insurance = 0
    for obj in objects:
        lender = ref["lender"].get(obj["lender_id"], {
            "insurance_rate_num": 0, "insurance_rate_den": 1, "pref_mode": "",
        })
        obj["insurance_cents"] = round_half_up(
            obj["declared_value_cents"] * lender["insurance_rate_num"],
            lender["insurance_rate_den"],
        )
        total_insurance += obj["insurance_cents"]

    audit_rows = [
        {
            "object_id": o["object_id"],
            "placed_bay": o["bay_id"] or "",
            "crate_class": o["crate_class"],
            "displaced": o["crate_code"] == "C_DISPLACED",
            "scheduled": o["window_code"] == "S_OK",
            "install_position": o["install_position"],
            "setup_seconds": o["setup_seconds"],
            "insurance_cents": o["insurance_cents"],
        }
        for o in objects
    ]

    unschedulable_penalty = sum(
        value_class_of(by_id[row["object_id"]], ref)["unschedulable_penalty"]
        for row in unschedulable_rows
    )
    contention_penalty = sum(
        value_class_of(o, ref)["contention_penalty"]
        for o in objects
        if o["crate_code"] == "C_DISPLACED"
    )
    total_residual = sum(
        o["crate_fits"][o["crate_class"]]
        for o in objects
        if o["crate_class"] in o["crate_fits"]
    )
    scheduled_count = sum(1 for o in objects if o["window_code"] == "S_OK")
    displacement_count = sum(1 for o in objects if o["crate_code"] == "C_DISPLACED")
    total_setup = sum(o["setup_seconds"] for o in objects)

    objective = [
        scheduled_count,
        unschedulable_penalty,
        contention_penalty,
        len(removed),
        total_residual,
    ]
    objective_names = list(
        load_json(os.path.join(data_dir, "objective_order.json")).get("order", [])
    )
    penalty_summary = {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": len(removed),
        "total_insurance_cents": total_insurance,
    }

    payloads = {
        "canonical_objects.jsonl": jsonl_bytes(canonical_rows),
        "normalized_geometry.jsonl": jsonl_bytes(geometry_rows),
        "admissibility_matrix.jsonl": jsonl_bytes(admissibility_rows),
        "bay_candidate_matrix.jsonl": jsonl_bytes(bay_candidate_rows),
        "selected_placement.jsonl": jsonl_bytes(selected_rows),
        "rejected_placement.jsonl": jsonl_bytes(rejected_rows),
        "conservation_adjusted.jsonl": jsonl_bytes(conservation_rows),
        "crate_candidate_matrix.jsonl": jsonl_bytes(crate_candidate_rows),
        "crate_assignment.jsonl": jsonl_bytes(assignment_rows),
        "crate_capacity_ledger.jsonl": jsonl_bytes(ledger_rows),
        "crate_contention_report.jsonl": jsonl_bytes(contention_rows),
        "transport_leg_manifest.jsonl": jsonl_bytes(transport_rows),
        "dependency_edges.jsonl": jsonl_bytes(edge_rows),
        "cycle_resolution_audit.jsonl": jsonl_bytes(removed),
        "install_order.jsonl": jsonl_bytes(install_rows),
        "crew_requirement_ledger.jsonl": jsonl_bytes(crew_rows),
        "install_window_table.jsonl": jsonl_bytes(window_rows),
        "unschedulable_report.jsonl": jsonl_bytes(unschedulable_rows),
        "gallery_occupancy_map.jsonl": jsonl_bytes(occupancy_rows),
        "deterministic_audit.jsonl": jsonl_bytes(audit_rows),
        "penalty_summary.json": json_bytes(penalty_summary),
        "objective_tuple.json": json_bytes({"objective": objective, "order": objective_names}),
    }

    os.makedirs(out_dir, exist_ok=True)
    program_path = os.path.abspath(__file__)
    target_program = os.path.join(out_dir, "plan.py")
    with open(program_path, "rb") as handle:
        program_bytes = handle.read()
    if os.path.abspath(target_program) != program_path:
        write_bytes(target_program, program_bytes)
    payloads["plan.py"] = program_bytes
    for name, payload in payloads.items():
        write_bytes(os.path.join(out_dir, name), payload)

    manifest = {name: sha256_hex(payload) for name, payload in payloads.items()}
    write_bytes(os.path.join(out_dir, "integrity_manifest.json"), json_bytes(manifest))

    chain = chain_seed
    for name in sorted(manifest):
        chain = sha256_hex((chain + manifest[name]).encode("utf-8"))

    final_state = {
        "chain": chain,
        "repair_converged": converged,
        "cycle_edges_removed": len(removed),
        "final_cycle_count": cycles_left,
        "displacement_count": displacement_count,
        "unschedulable_count": len(unschedulable_rows),
        "placed_objects": scheduled_count,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    }
    write_bytes(os.path.join(out_dir, "final_state.json"), json_bytes(final_state))
    return final_state


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    state = derive(argv[1], argv[2])
    sys.stdout.write(
        "placed_objects={0} unschedulable={1} displaced={2} cycles_broken={3} "
        "objective={4}\n".format(
            state["placed_objects"],
            state["unschedulable_count"],
            state["displacement_count"],
            state["cycle_edges_removed"],
            state["objective"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
