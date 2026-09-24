#!/usr/bin/env python3
"""Derive the travelling exhibition installation dossier from a snapshot.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Everything is derived from the records read from DATA_DIR alone; no data or
output path is fixed inside this program. All arithmetic is integer or exact
rational, rounded half up where a stage calls for it; no floating point value
is computed, compared or written. Two runs over the same input bytes produce
identical output bytes. Standard library only.
"""

import csv
import hashlib
import json
import os
import sys

MM_UM = 1000            # millimetre -> micrometre
CM_UM = 10000           # centimetre -> micrometre
IN_UM = 25400           # inch -> micrometre
KG_G = 1000             # kilogram -> gram
G_G = 1
VALUE_CENTS = 100       # declared value -> cents
DEFAULT_SETUP_DIV = 700
SECONDS_PER_DAY = 3600
DEFAULT_CHAIN_SEED = "0" * 64

# Orientation index -> (footprint width, footprint depth, vertical extent),
# each a key of the normalized clearance geometry.
ORIENT_SHAPES = {0: ("cw", "cd", "ch"), 1: ("cd", "cw", "ch"),
                 2: ("cw", "ch", "cd"), 3: ("ch", "cw", "cd")}

# Removal precedence: a bay size edge goes before a conservation edge before
# an explicit rule.
KIND_RANK = {"D_BAYSIZE": 1, "D_CONSERVATION": 2, "D_RULE": 3}


# ---------------------------------------------------------------------------
# exact rational helpers


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a or 1


def round_half_up(num, den):
    """Round the exact rational num/den half up (halfway goes to the larger)."""
    if den < 0:
        num, den = -num, -den
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def reduced_pair(num, den):
    """Reduce num/den to lowest terms; a zero denominator yields 0/1."""
    if den == 0:
        return 0, 1
    if den < 0:
        num, den = -num, -den
    g = gcd(abs(num), den)
    return num // g, den // g


# ---------------------------------------------------------------------------
# input


def read_csv_rows(data_dir, name):
    with open(os.path.join(data_dir, name), newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def load_json(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def as_int(text):
    return int(("" if text is None else str(text)).strip())


def opt_int(row, key, default=0):
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return default
    return int(str(value).strip())


def length_factor(code):
    code = ("" if code is None else str(code)).strip().upper()
    return {"MM": MM_UM, "CM": CM_UM, "IN": IN_UM}.get(code, 1)


def mass_factor(code):
    return KG_G if ("" if code is None else str(code)).strip().upper() == "KG" else G_G


# ---------------------------------------------------------------------------
# output serialization


def jsonl_bytes(rows):
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows]
    return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")


def obj_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# stages


def read_inputs(data_dir):
    horizon = load_json(data_dir, "horizon_config.json")
    return {
        "horizon": horizon,
        "setup_div": as_int(horizon.get("setup_div", DEFAULT_SETUP_DIV)),
        "chain_seed": str(horizon.get("chain_seed", DEFAULT_CHAIN_SEED)),
        "master_rows": read_csv_rows(data_dir, "object_master.csv"),
        "version_rows": read_csv_rows(data_dir, "object_version_map.csv"),
        "dim_rows": read_csv_rows(data_dir, "object_dimensions.csv"),
        "mass_rows": read_csv_rows(data_dir, "object_mass.csv"),
        "value_rows": read_csv_rows(data_dir, "object_value.csv"),
        "orientation_rows": read_csv_rows(data_dir, "object_orientation.csv"),
        "material_rows": read_csv_rows(data_dir, "material_rules.csv"),
        "handling_rows": read_csv_rows(data_dir, "handling_class_rules.csv"),
        "bay_rows": read_csv_rows(data_dir, "gallery_bays.csv"),
        "bay_exception_rows": read_csv_rows(data_dir, "bay_exceptions.csv"),
        "conservation_rows": read_csv_rows(data_dir, "conservation_rules.csv"),
        "crate_rows": read_csv_rows(data_dir, "crate_catalog.csv"),
        "reservation_rows": read_csv_rows(data_dir, "crate_reservations.csv"),
        "prior_rows": read_csv_rows(data_dir, "prior_state.csv"),
        "venue_rows": read_csv_rows(data_dir, "venue_windows.csv"),
        "dependency_rows": read_csv_rows(data_dir, "dependency_rules.csv"),
        "crew_rows": read_csv_rows(data_dir, "crew_roster.csv"),
        "transport_rows": read_csv_rows(data_dir, "transport_rules.csv"),
        "lender_rows": read_csv_rows(data_dir, "lender_master.csv"),
        "value_class_rows": read_csv_rows(data_dir, "value_class_rules.csv"),
        "objective_order": load_json(data_dir, "objective_order.json"),
    }


def stage_1_identity(inp):
    """Canonical identity, resolved version and converted units."""
    order = []
    master = {}
    for row in inp["master_rows"]:
        oid = row["object_id"].strip()
        if oid in master:
            continue          # stale duplicate: the first occurrence wins
        order.append(oid)
        master[oid] = row

    versions = {}
    for row in inp["version_rows"]:
        oid = row["object_id"].strip()
        v = as_int(row["version"])
        if v > versions.get(oid, v - 1):
            versions[oid] = v

    dims = {r["object_id"].strip(): r for r in inp["dim_rows"]}
    masses = {r["object_id"].strip(): r for r in inp["mass_rows"]}
    values = {r["object_id"].strip(): r for r in inp["value_rows"]}
    orients = {r["object_id"].strip(): r for r in inp["orientation_rows"]}
    materials = {r["material_code"].strip(): r for r in inp["material_rows"]}
    handling = {r["handling_class"].strip(): r for r in inp["handling_rows"]}

    objects = {}
    for oid in order:
        mrow = master[oid]
        lender = mrow["lender_id"].strip()
        material = mrow["material_code"].strip()
        mrule = materials.get(material, {})
        hclass = mrule.get("handling_class", "").strip() if mrule else ""
        hrule = handling.get(hclass, {})
        drow = dims.get(oid, {})
        factor = length_factor(drow.get("dim_unit"))
        w = as_int(drow.get("width")) * factor
        h = as_int(drow.get("height")) * factor
        dep = as_int(drow.get("depth")) * factor
        mrow_mass = masses.get(oid, {})
        mass = as_int(mrow_mass.get("mass")) * mass_factor(mrow_mass.get("mass_unit"))
        cents = as_int(values.get(oid, {}).get("declared_value")) * VALUE_CENTS
        orow = orients.get(oid, {})
        objects[oid] = {
            "object_id": oid,
            "lender_id": lender,
            "material_code": material,
            "version": versions.get(oid, 0),
            "declared_value_cents": cents,
            "mass_g": mass,
            "conservation_class": mrule.get("conservation_class", "").strip() if mrule else "",
            "max_lux": opt_int(mrule, "max_lux"),
            "handling_class": hclass,
            "crew_class": hrule.get("crew_class", "").strip() if hrule else "",
            "setup_base": opt_int(hrule, "setup_base"),
            "margin_w": opt_int(hrule, "clearance_w") * MM_UM,
            "margin_d": opt_int(hrule, "clearance_d") * MM_UM,
            "margin_h": opt_int(hrule, "clearance_h") * MM_UM,
            "handle_margin": opt_int(hrule, "handle_margin") * MM_UM,
            "tip_allowed": as_int(orow.get("tip_allowed", 0)) != 0,
            "rotate_allowed": as_int(orow.get("rotate_allowed", 0)) != 0,
            "w": w, "h": h, "d": dep,
        }
    return order, objects


def stage_2_geometry(order, objects):
    """Clearance envelope in micrometres and the reduced aspect ratio."""
    geometry = {}
    rows = []
    for oid in order:
        o = objects[oid]
        cw = o["w"] + 2 * o["margin_w"]
        cd = o["d"] + 2 * o["margin_d"]
        ch = o["h"] + o["margin_h"]
        num, den = reduced_pair(o["h"], o["w"])
        geometry[oid] = {"cw": cw, "cd": cd, "ch": ch}
        rows.append({"object_id": oid, "clearance_w": cw, "clearance_h": ch,
                     "clearance_d": cd, "aspect_num": num, "aspect_den": den})
    return geometry, rows


def stage_3_admissibility(order, objects):
    """Admissible orientation indices in the fixed 0, 1, 2, 3 convention."""
    admissible = {}
    rows = []
    for oid in order:
        o = objects[oid]
        lst = [0]
        if o["rotate_allowed"]:
            lst.append(1)
        if o["tip_allowed"]:
            lst.extend([2, 3])
        admissible[oid] = lst
        rows.append({"object_id": oid, "orientations": lst,
                     "tip_allowed": o["tip_allowed"],
                     "rotate_allowed": o["rotate_allowed"]})
    return admissible, rows


def read_bays(inp):
    bays = []
    for r in inp["bay_rows"]:
        bays.append({
            "bay_id": r["bay_id"].strip(),
            "w": as_int(r["interior_w"]) * MM_UM,
            "h": as_int(r["interior_h"]) * MM_UM,
            "d": as_int(r["interior_d"]) * MM_UM,
            "lux": as_int(r["lux_level"]),
            "priority": as_int(r["bay_priority"]),
        })
    return bays


def read_conservation(inp):
    rules = {}
    for r in inp["conservation_rows"]:
        rules[r["conservation_class"].strip()] = {
            "setup_start": as_int(r["setup_start"]),
            "setup_end": as_int(r["setup_end"]),
            "needs_dark": as_int(r["needs_dark"]) != 0,
        }
    return rules


def read_crates(inp):
    crates = []
    for r in inp["crate_rows"]:
        dims = sorted([as_int(r["inner_w"]) * MM_UM,
                       as_int(r["inner_h"]) * MM_UM,
                       as_int(r["inner_d"]) * MM_UM])
        crates.append({
            "crate_class": r["crate_class"].strip(),
            "dims": dims,
            "volume": dims[0] * dims[1] * dims[2],
            "count": as_int(r["count"]),
            "mode": r["transport_mode"].strip(),
        })
    crates.sort(key=lambda c: c["crate_class"])
    return crates


def derive(data_dir, program_path):
    inp = read_inputs(data_dir)
    order, objects = stage_1_identity(inp)
    geometry, geometry_rows = stage_2_geometry(order, objects)
    admissible, admissibility_rows = stage_3_admissibility(order, objects)
    bays = read_bays(inp)
    bay_order = sorted(bays, key=lambda b: b["bay_id"])
    bay_by_id = {b["bay_id"]: b for b in bays}
    scan_order = sorted(bays, key=lambda b: (b["priority"], b["bay_id"]))

    # ---- stage 4: bay candidate matrix
    def smallest_fitting_orientation(oid, bay):
        g = geometry[oid]
        for o in admissible[oid]:
            fw, fd, ve = ORIENT_SHAPES[o]
            if g[fw] <= bay["w"] and g[fd] < bay["d"] and g[ve] <= bay["h"]:
                return o
        return None

    bay_candidate_rows = []
    fit_bays = {}
    for oid in order:
        entry = {}
        for bay in bay_order:
            o = smallest_fitting_orientation(oid, bay)
            entry[bay["bay_id"]] = o
            bay_candidate_rows.append({"object_id": oid, "bay_id": bay["bay_id"],
                                       "fits": o is not None,
                                       "orientation": -1 if o is None else o})
        fit_bays[oid] = entry

    # ---- stage 5: selected placement, geometry only
    def geometric_scan(oid):
        for bay in scan_order:
            o = fit_bays[oid][bay["bay_id"]]
            if o is not None:
                return bay["bay_id"], o
        return None, None

    selected = {}
    selected_rows = []
    rejected_rows = []
    for oid in order:
        bay_id, o = geometric_scan(oid)
        if bay_id is None:
            code = "P_NO_ORIENTATION" if admissible[oid] == [0] else "P_NO_FIT"
            selected[oid] = {"bay_id": None, "orientation": -1, "code": code}
            selected_rows.append({"object_id": oid, "bay_id": "",
                                  "orientation": -1, "code": code})
            rejected_rows.append({"object_id": oid, "code": code})
        else:
            selected[oid] = {"bay_id": bay_id, "orientation": o, "code": "P_OK"}
            selected_rows.append({"object_id": oid, "bay_id": bay_id,
                                  "orientation": o, "code": "P_OK"})
    rejected_rows.sort(key=lambda r: (r["object_id"], r["code"]))

    # ---- stage 6: conservation adjustment
    blocked = {(r["bay_id"].strip(), r["blocked_class"].strip())
               for r in inp["bay_exception_rows"]}
    conservation_rules = read_conservation(inp)
    needs_dark = {c for c, r in conservation_rules.items() if r["needs_dark"]}
    sensitivity_rank = {c: i for i, c in enumerate(sorted(conservation_rules))}

    def passes_conservation(oid, bay_id):
        bay = bay_by_id[bay_id]
        obj = objects[oid]
        cls = obj["conservation_class"]
        if bay["lux"] > obj["max_lux"]:
            return False
        if (bay_id, cls) in blocked:
            return False
        if cls in needs_dark and bay["lux"] > 0:
            return False
        return True

    placed = {}
    conservation_rows = []
    for oid in order:
        chosen = None
        for bay in scan_order:
            o = fit_bays[oid][bay["bay_id"]]
            if o is None or not passes_conservation(oid, bay["bay_id"]):
                continue
            chosen = (bay["bay_id"], o)
            break
        if chosen is None:
            code = selected[oid]["code"] if selected[oid]["bay_id"] is None \
                else "P_CONSERVATION_BLOCK"
            placed[oid] = {"bay_id": None, "orientation": -1, "code": code}
            conservation_rows.append({"object_id": oid, "bay_id": "",
                                      "orientation": -1, "code": code})
        else:
            placed[oid] = {"bay_id": chosen[0], "orientation": chosen[1],
                           "code": "P_OK"}
            conservation_rows.append({"object_id": oid, "bay_id": chosen[0],
                                      "orientation": chosen[1], "code": "P_OK"})
    placed_ids = [oid for oid in order if placed[oid]["bay_id"] is not None]

    # ---- stage 7: crate candidates
    crates = read_crates(inp)
    crate_by_class = {c["crate_class"]: c for c in crates}
    envelope = {}
    handling_volume = {}
    for oid in order:
        o = objects[oid]
        m = o["handle_margin"]
        dims = sorted([o["w"] + m, o["h"] + m, o["d"] + m])
        envelope[oid] = dims
        handling_volume[oid] = dims[0] * dims[1] * dims[2]

    fitting = {}
    primary = {}
    crate_candidate_rows = []
    for oid in order:
        fits = []
        for crate in crates:
            hd, cd = envelope[oid], crate["dims"]
            if hd[0] <= cd[0] and hd[1] <= cd[1] and hd[2] <= cd[2]:
                fits.append((crate["volume"] - handling_volume[oid],
                             crate["crate_class"]))
        fits.sort()
        fitting[oid] = fits
        primary[oid] = fits[0][1] if fits else None
        for residual, cls in fits:
            crate_candidate_rows.append({"object_id": oid, "crate_class": cls,
                                         "residual": residual})

    # ---- setup seconds (stage 13 quantity, consumed by the install order)
    setup_seconds = {oid: round_half_up(objects[oid]["setup_base"] * objects[oid]["mass_g"],
                                        inp["setup_div"]) for oid in order}

    # ---- stage 10: dependency edges
    edges = {}
    for r in inp["dependency_rows"]:
        a, b = r["from_object"].strip(), r["to_object"].strip()
        if a in objects and b in objects and a != b:
            edges[(a, b)] = {"kind": "D_RULE", "weight": as_int(r["edge_priority"])}

    by_bay = {}
    for oid in placed_ids:
        by_bay.setdefault(placed[oid]["bay_id"], []).append(oid)
    for bay_id in sorted(by_bay):
        ids = by_bay[bay_id]
        for a in ids:
            for b in ids:
                if a == b:
                    continue
                if handling_volume[a] > handling_volume[b] or (
                        handling_volume[a] == handling_volume[b] and a < b):
                    edges.setdefault((a, b), {"kind": "D_BAYSIZE",
                                              "weight": handling_volume[a]})
                rank_a = sensitivity_rank.get(objects[a]["conservation_class"])
                rank_b = sensitivity_rank.get(objects[b]["conservation_class"])
                if rank_a is not None and rank_b is not None and rank_a > rank_b:
                    cur = edges.get((a, b))
                    if cur is None or KIND_RANK[cur["kind"]] < KIND_RANK["D_CONSERVATION"]:
                        edges[(a, b)] = {"kind": "D_CONSERVATION",
                                         "weight": handling_volume[a]}

    edge_list = sorted(
        ({"from": k[0], "to": k[1], "kind": v["kind"], "weight": v["weight"]}
         for k, v in edges.items()),
        key=lambda e: (e["from"], e["to"]))

    # ---- stage 11: cycle resolution
    removed_edges = []
    while True:
        cycle = find_cycle(order, edge_list)
        if cycle is None:
            break
        pool = [e for e in edge_list if (e["from"], e["to"]) in cycle]
        drop = min(pool, key=lambda e: (KIND_RANK[e["kind"]], e["weight"],
                                        e["from"], e["to"]))
        edge_list.remove(drop)
        removed_edges.append(drop)

    successor = {oid: [] for oid in order}
    unmet = {oid: 0 for oid in order}
    for e in edge_list:
        successor[e["from"]].append(e["to"])
        unmet[e["to"]] += 1

    # ---- stage 12: install order
    install_order = []
    position = {}
    ready = [oid for oid in order if unmet[oid] == 0]
    while ready:
        oid = min(ready, key=lambda o: (setup_seconds[o], o))
        ready.remove(oid)
        position[oid] = len(install_order) + 1
        install_order.append(oid)
        for nxt in successor[oid]:
            unmet[nxt] -= 1
            if unmet[nxt] == 0:
                ready.append(nxt)
    for oid in order:        # defensive: cannot happen once every cycle is broken
        if oid not in position:
            position[oid] = len(install_order) + 1
            install_order.append(oid)
    install_order_rows = [{"position": i + 1, "object_id": oid}
                          for i, oid in enumerate(install_order)]

    # ---- stage 8: shared crate capacity
    reservations = {}
    for r in inp["reservation_rows"]:
        key = (r["crate_class"].strip(), r["lender_id"].strip())
        reservations[key] = reservations.get(key, 0) + as_int(r["reserved_count"])
    prior_consumed = {}
    for r in inp["prior_rows"]:
        cls = r["crate_class"].strip()
        prior_consumed[cls] = prior_consumed.get(cls, 0) + as_int(r["consumed_count"])
    # availability is the catalogue count less prior consumption, never negative
    remaining = {c["crate_class"]: max(0, c["count"] - prior_consumed.get(c["crate_class"], 0))
                 for c in crates}

    assignment = {}
    for oid in install_order:
        fits = fitting[oid]
        available = [(residual, cls) for residual, cls in fits
                     if remaining.get(cls, 0) > 0]
        if not available:
            assignment[oid] = None
            continue
        lender = objects[oid]["lender_id"]
        available_classes = {cls for _, cls in available}
        reserved = {cls for cls in available_classes
                    if reservations.get((cls, lender), 0) > 0}
        if reserved:
            residual_of = {cls: residual for residual, cls in fits}
            chosen = min(reserved, key=lambda cls: (residual_of[cls], cls))
            reservations[(chosen, lender)] -= 1
        else:
            chosen = available[0][1]
        remaining[chosen] -= 1
        assignment[oid] = chosen

    crate_assignment_rows = []
    displaced_ids = []
    for oid in sorted(order):
        cls = assignment.get(oid)
        if cls is None:
            code = "C_NO_CRATE"
        elif cls == primary[oid]:
            code = "C_PRIMARY"
        else:
            code = "C_DISPLACED"
            displaced_ids.append(oid)
        crate_assignment_rows.append({
            "object_id": oid,
            "crate_class": "" if cls is None else cls,
            "code": code,
            "primary_class": "" if primary[oid] is None else primary[oid],
        })
    displaced_set = set(displaced_ids)
    crate_contention_rows = [{"object_id": oid, "wanted_class": primary[oid],
                              "assigned_class": assignment[oid]}
                             for oid in displaced_ids]
    capacity_rows = [{"crate_class": c["crate_class"],
                      "catalog_count": c["count"],
                      "prior_consumed": prior_consumed.get(c["crate_class"], 0),
                      "remaining": remaining[c["crate_class"]]}
                     for c in crates]

    # ---- stage 9: transport legs
    transport = {r["lender_id"].strip(): {"forbidden": r["forbidden_mode"].strip(),
                                          "fallback": r["fallback_mode"].strip()}
                 for r in inp["transport_rows"]}
    transport_rows = []
    for oid in order:
        cls = assignment.get(oid)
        if cls is None:
            continue
        mode = crate_by_class[cls]["mode"]
        exception = False
        rule = transport.get(objects[oid]["lender_id"])
        if rule is not None and rule["forbidden"] == mode:
            mode = rule["fallback"]
            exception = True
        transport_rows.append({"object_id": oid, "crate_class": cls,
                               "mode": mode, "exception": exception})

    # ---- stage 13: crew, setup and install windows
    crew = {r["crew_class"].strip(): {"start": as_int(r["avail_start"]),
                                      "end": as_int(r["avail_end"])}
            for r in inp["crew_rows"]}
    venue = {r["bay_id"].strip(): {"start": as_int(r["access_start"]),
                                   "end": as_int(r["access_end"])}
             for r in inp["venue_rows"]}
    lenders = {r["lender_id"].strip(): r for r in inp["lender_rows"]}
    value_classes = sorted(
        ({"value_class": r["value_class"].strip(),
          "min_value": as_int(r["min_value"]),
          "unschedulable_penalty": as_int(r["unschedulable_penalty"]),
          "contention_penalty": as_int(r["contention_penalty"])}
         for r in inp["value_class_rows"]),
        key=lambda v: -v["min_value"])
    value_rule = {v["value_class"]: v for v in value_classes}

    def value_class_of(oid):
        declared = objects[oid]["declared_value_cents"]
        for v in value_classes:
            if declared >= v["min_value"] * VALUE_CENTS:
                return v["value_class"]
        return value_classes[-1]["value_class"] if value_classes else ""

    insurance_cents = {}
    for oid in order:
        lrow = lenders.get(objects[oid]["lender_id"])
        if lrow is None:
            insurance_cents[oid] = 0
        else:
            insurance_cents[oid] = round_half_up(
                objects[oid]["declared_value_cents"] * as_int(lrow["insurance_rate_num"]),
                as_int(lrow["insurance_rate_den"]))

    window_rows = []
    unschedulable_rows = []
    scheduled = {}
    for oid in order:
        bay_id = placed[oid]["bay_id"]
        cls = assignment.get(oid)
        if bay_id is None:
            code, row_bay, low, high, required = "S_NO_PLACEMENT", "", 0, -1, 0
        elif cls is None:
            code, row_bay, low, high, required = "S_NO_CRATE", bay_id, 0, -1, 0
        else:
            cons = conservation_rules.get(objects[oid]["conservation_class"], {})
            vw = venue.get(bay_id, {"start": 0, "end": -1})
            cr = crew.get(objects[oid]["crew_class"], {"start": 0, "end": -1})
            low = max(opt_int(cons, "setup_start"), vw["start"], cr["start"])
            high = min(opt_int(cons, "setup_end", -1), vw["end"] - 1, cr["end"])
            days = high - low + 1
            required = -(-setup_seconds[oid] // SECONDS_PER_DAY)
            if days <= 0:
                code = "S_NO_WINDOW"
            elif required > days:
                code = "S_SHORT_WINDOW"
            else:
                code = "S_OK"
            row_bay = bay_id
        row = {"object_id": oid, "bay_id": row_bay, "window_start": low,
               "window_end": high, "required_days": required, "code": code}
        window_rows.append(row)
        scheduled[oid] = code == "S_OK"
        if code != "S_OK":
            unschedulable_rows.append({"object_id": oid, "code": code})
    unschedulable_rows.sort(key=lambda r: (r["object_id"], r["code"]))
    crew_rows = [{"object_id": oid, "crew_class": objects[oid]["crew_class"],
                  "setup_seconds": setup_seconds[oid]} for oid in order]

    # ---- stage 14: dossier
    occupancy_rows = []
    for bay in bay_order:
        ids = sorted(oid for oid in placed_ids if placed[oid]["bay_id"] == bay["bay_id"])
        occupancy_rows.append({"bay_id": bay["bay_id"], "object_count": len(ids),
                               "objects": ids})

    unschedulable_penalty = sum(
        value_rule[value_class_of(r["object_id"])]["unschedulable_penalty"]
        for r in unschedulable_rows if value_class_of(r["object_id"]) in value_rule)
    contention_penalty = sum(
        value_rule[value_class_of(oid)]["contention_penalty"]
        for oid in displaced_ids if value_class_of(oid) in value_rule)

    total_residual = 0
    for oid in order:
        cls = assignment.get(oid)
        if cls is not None:
            total_residual += {c: residual for residual, c in fitting[oid]}[cls]
    cycles_broken = len(removed_edges)
    total_setup = sum(setup_seconds.values())
    total_insurance = sum(insurance_cents.values())
    placed_objects = sum(1 for oid in order if scheduled.get(oid))

    deterministic_rows = [{
        "object_id": oid,
        "placed_bay": placed[oid]["bay_id"] or "",
        "crate_class": assignment.get(oid) or "",
        "displaced": oid in displaced_set,
        "scheduled": scheduled.get(oid, False),
        "install_position": position[oid],
        "setup_seconds": setup_seconds[oid],
        "insurance_cents": insurance_cents[oid],
    } for oid in order]

    objective = [placed_objects, unschedulable_penalty, contention_penalty,
                 cycles_broken, total_residual]
    objective_order_cfg = inp["objective_order"]
    default_order = ["placed_objects", "unschedulable_penalty", "contention_penalty",
                     "cycles_broken", "total_residual"]

    artifacts = {
        "canonical_objects.jsonl": jsonl_bytes([
            {"object_id": oid, "lender_id": objects[oid]["lender_id"],
             "material_code": objects[oid]["material_code"],
             "version": objects[oid]["version"],
             "declared_value_cents": objects[oid]["declared_value_cents"],
             "mass_g": objects[oid]["mass_g"]} for oid in order]),
        "normalized_geometry.jsonl": jsonl_bytes(geometry_rows),
        "admissibility_matrix.jsonl": jsonl_bytes(admissibility_rows),
        "bay_candidate_matrix.jsonl": jsonl_bytes(bay_candidate_rows),
        "selected_placement.jsonl": jsonl_bytes(selected_rows),
        "rejected_placement.jsonl": jsonl_bytes(rejected_rows),
        "conservation_adjusted.jsonl": jsonl_bytes(conservation_rows),
        "crate_candidate_matrix.jsonl": jsonl_bytes(crate_candidate_rows),
        "crate_assignment.jsonl": jsonl_bytes(crate_assignment_rows),
        "crate_capacity_ledger.jsonl": jsonl_bytes(capacity_rows),
        "crate_contention_report.jsonl": jsonl_bytes(crate_contention_rows),
        "transport_leg_manifest.jsonl": jsonl_bytes(transport_rows),
        "dependency_edges.jsonl": jsonl_bytes(edge_list),
        "cycle_resolution_audit.jsonl": jsonl_bytes(removed_edges),
        "install_order.jsonl": jsonl_bytes(install_order_rows),
        "crew_requirement_ledger.jsonl": jsonl_bytes(crew_rows),
        "install_window_table.jsonl": jsonl_bytes(window_rows),
        "unschedulable_report.jsonl": jsonl_bytes(unschedulable_rows),
        "gallery_occupancy_map.jsonl": jsonl_bytes(occupancy_rows),
        "deterministic_audit.jsonl": jsonl_bytes(deterministic_rows),
        "penalty_summary.json": obj_bytes({
            "unschedulable_penalty": unschedulable_penalty,
            "contention_penalty": contention_penalty,
            "cycles_broken": cycles_broken,
            "total_insurance_cents": total_insurance}),
        "objective_tuple.json": obj_bytes({
            "objective": objective,
            "order": objective_order_cfg.get("order", default_order) if isinstance(
                objective_order_cfg, dict) else default_order}),
    }

    with open(program_path, "rb") as fh:
        program_digest = hashlib.sha256(fh.read()).hexdigest()
    manifest = {name: hashlib.sha256(payload).hexdigest()
                for name, payload in artifacts.items()}
    manifest[os.path.basename(program_path)] = program_digest
    artifacts["integrity_manifest.json"] = obj_bytes(manifest)

    chain = inp["chain_seed"]
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    final_cycle_count = 0 if find_cycle(order, edge_list) is None else 1
    artifacts["final_state.json"] = obj_bytes({
        "chain": chain,
        "repair_converged": final_cycle_count == 0,
        "cycle_edges_removed": cycles_broken,
        "final_cycle_count": final_cycle_count,
        "displacement_count": len(displaced_ids),
        "unschedulable_count": len(unschedulable_rows),
        "placed_objects": placed_objects,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective})
    return artifacts


def find_cycle(nodes, edge_list):
    """One cycle as a set of (from, to) pairs, or None when acyclic.

    Depth first search over the nodes in their given order with successors
    visited in ascending order, so the same graph always yields the same cycle.
    """
    known = set(nodes)
    successor = {n: [] for n in nodes}
    for e in edge_list:
        if e["from"] in successor and e["to"] in known:
            successor[e["from"]].append(e["to"])
    for n in nodes:
        successor[n].sort()
    WHITE, GREY, BLACK = 0, 1, 2
    state = {n: WHITE for n in nodes}
    for root in nodes:
        if state[root] != WHITE:
            continue
        stack = [(root, iter(successor[root]))]
        state[root] = GREY
        path = [root]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if state[nxt] == GREY:
                idx = len(path) - 1
                while idx >= 0 and path[idx] != nxt:
                    idx -= 1
                if idx < 0:
                    continue
                cycle_nodes = path[idx:]
                pairs = {(cycle_nodes[i], cycle_nodes[i + 1])
                         for i in range(len(cycle_nodes) - 1)}
                pairs.add((cycle_nodes[-1], nxt))
                return pairs
            if state[nxt] == WHITE:
                state[nxt] = GREY
                stack.append((nxt, iter(successor[nxt])))
                path.append(nxt)
    return None


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(data_dir):
        sys.stderr.write("not a directory: %s\n" % data_dir)
        return 2
    program_path = os.path.abspath(__file__)
    artifacts = derive(data_dir, program_path)
    os.makedirs(out_dir, exist_ok=True)
    for name, payload in artifacts.items():
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
