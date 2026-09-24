#!/usr/bin/env python3
"""Installation dossier derivation.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Derives the twenty four dossier artifacts from a snapshot directory using only
integer and exact rational arithmetic, per the specification shipped with the
snapshot (rules.md, schema.md, constants.md, reason_codes.md).
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction
from math import ceil, gcd

# Fixed scale factors and quanta from constants.md.
LENGTH_FACTOR = {'MM': 1000, 'CM': 10000, 'IN': 25400}
MASS_FACTOR = {'G': 1, 'KG': 1000}
SECONDS_PER_DAY = 3600

# Dependency kinds in ascending removal precedence (removed first first).
KIND_RANK = {'D_BAYSIZE': 0, 'D_CONSERVATION': 1, 'D_RULE': 2}


def sensitivity(class_code):
    """Conservation sensitivity rank; lower means more sensitive."""
    digits = ''.join(ch for ch in str(class_code) if ch.isdigit())
    if digits:
        return -int(digits)
    return str(class_code)


def ratio(num, den):
    """Exact ratio; a zero denominator yields the sentinel zero over one."""
    if den == 0:
        return Fraction(0)
    return Fraction(num, den)


def round_half_up(numerator, denominator, quantum=1):
    """Round numerator/denominator times quantum to an integer, half up."""
    total = Fraction(numerator, denominator if denominator else 1) * quantum
    quotient, remainder = divmod(total.numerator, total.denominator)
    if 2 * remainder >= total.denominator:
        quotient += 1
    return quotient


def reduced_pair(num, den):
    if den == 0:
        return 0, 1
    g = gcd(num, den)
    return num // g, den // g


def read_rows(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_json(data_dir, name):
    path = os.path.join(data_dir, name)
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


class Snapshot(object):
    """Parsed and typed snapshot records."""

    def __init__(self, data_dir):
        self.horizon = load_json(data_dir, 'horizon_config.json')
        self.objective_order = load_json(data_dir, 'objective_order.json')
        self.master_rows = read_rows(data_dir, 'object_master.csv')
        self.dimensions = {r['object_id']: r for r in read_rows(data_dir, 'object_dimensions.csv')}
        self.mass = {r['object_id']: r for r in read_rows(data_dir, 'object_mass.csv')}
        self.value = {r['object_id']: r for r in read_rows(data_dir, 'object_value.csv')}
        self.orientation = {r['object_id']: r for r in read_rows(data_dir, 'object_orientation.csv')}
        self.version_map = read_rows(data_dir, 'object_version_map.csv')
        self.materials = {r['material_code']: r for r in read_rows(data_dir, 'material_rules.csv')}
        self.handling = {r['handling_class']: r for r in read_rows(data_dir, 'handling_class_rules.csv')}
        self.bays = read_rows(data_dir, 'gallery_bays.csv')
        self.bay_exceptions = read_rows(data_dir, 'bay_exceptions.csv')
        self.crates = read_rows(data_dir, 'crate_catalog.csv')
        self.reservations = read_rows(data_dir, 'crate_reservations.csv')
        self.prior_state = read_rows(data_dir, 'prior_state.csv')
        self.conservation = {r['conservation_class']: r for r in read_rows(data_dir, 'conservation_rules.csv')}
        self.venue_windows = {r['bay_id']: r for r in read_rows(data_dir, 'venue_windows.csv')}
        self.dependency_rules = read_rows(data_dir, 'dependency_rules.csv')
        self.crew = {r['crew_class']: r for r in read_rows(data_dir, 'crew_roster.csv')}
        self.transport = {r['lender_id']: r for r in read_rows(data_dir, 'transport_rules.csv')}
        self.lenders = {r['lender_id']: r for r in read_rows(data_dir, 'lender_master.csv')}
        self.value_classes = read_rows(data_dir, 'value_class_rules.csv')


def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: plan.py <DATA_DIR> <OUT_DIR>\n')
        return 2
    data_dir, out_dir = argv[1], argv[2]
    snap = Snapshot(data_dir)
    setup_div = int(snap.horizon['setup_div'])
    venue_end_exclusive = snap.horizon.get('venue_end_convention') == 'exclusive'
    cons_end_inclusive = snap.horizon.get('conservation_end_convention') == 'inclusive'

    # ---- Stage 1: canonical identity and units -----------------------------
    objects = []
    seen_ids = set()
    for row in snap.master_rows:
        oid = row['object_id']
        if oid in seen_ids:
            continue
        seen_ids.add(oid)
        dim = snap.dimensions[oid]
        factor = LENGTH_FACTOR[dim['dim_unit']]
        width = int(dim['width']) * factor
        height = int(dim['height']) * factor
        depth = int(dim['depth']) * factor
        mass_row = snap.mass[oid]
        mass_g = int(mass_row['mass']) * MASS_FACTOR[mass_row['mass_unit']]
        material = snap.materials[row['material_code']]
        obj = {
            'object_id': oid,
            'lender_id': row['lender_id'],
            'material_code': row['material_code'],
            'version': 0,
            'declared_value_cents': int(snap.value[oid]['declared_value']) * 100,
            'mass_g': mass_g,
            'width': width,
            'height': height,
            'depth': depth,
            'conservation_class': material['conservation_class'],
            'max_lux': int(material['max_lux']),
            'handling_class': material['handling_class'],
        }
        objects.append(obj)
    object_ids = [o['object_id'] for o in objects]
    by_id = {o['object_id']: o for o in objects}

    version = {}
    for row in snap.version_map:
        oid = row['object_id']
        value = int(row['version'])
        version[oid] = max(version.get(oid, value), value)
    for obj in objects:
        obj['version'] = version.get(obj['object_id'], 0)

    # ---- Stage 2: normalized geometry -------------------------------------
    blocked_classes = {}
    for row in snap.bay_exceptions:
        blocked_classes.setdefault(row['bay_id'], set()).add(row['blocked_class'])

    needs_dark = {}
    for code, rule in snap.conservation.items():
        needs_dark[code] = int(rule.get('needs_dark', 0)) == 1

    for obj in objects:
        rules = snap.handling[obj['handling_class']]
        cw_margin = int(rules['clearance_w']) * 1000
        cd_margin = int(rules['clearance_d']) * 1000
        ch_margin = int(rules['clearance_h']) * 1000
        obj['clearance_w'] = obj['width'] + 2 * cw_margin
        obj['clearance_d'] = obj['depth'] + 2 * cd_margin
        obj['clearance_h'] = obj['height'] + ch_margin
        obj['aspect_num'], obj['aspect_den'] = reduced_pair(obj['height'], obj['width'])
        handle_margin = int(rules['handle_margin']) * 1000
        obj['handle_dims'] = sorted([
            obj['width'] + 2 * handle_margin,
            obj['height'] + 2 * handle_margin,
            obj['depth'] + 2 * handle_margin,
        ])
        obj['handling_volume'] = obj['handle_dims'][0] * obj['handle_dims'][1] * obj['handle_dims'][2]
        obj['crew_class'] = rules['crew_class']
        obj['setup_base'] = int(rules['setup_base'])
        obj['setup_seconds'] = round_half_up(
            obj['setup_base'] * obj['mass_g'], setup_div)

    # ---- Stage 3: orientation admissibility -------------------------------
    orientation_rows = []
    for obj in objects:
        row = snap.orientation[obj['object_id']]
        tip = int(row['tip_allowed']) == 1
        rotate = int(row['rotate_allowed']) == 1
        orientations = [0]
        if rotate:
            orientations.append(1)
        if tip:
            orientations.extend([2, 3])
        obj['orientations'] = orientations
        obj['tip_allowed'] = tip
        obj['rotate_allowed'] = rotate
        orientation_rows.append({
            'object_id': obj['object_id'],
            'orientations': orientations,
            'tip_allowed': tip,
            'rotate_allowed': rotate,
        })

    def footprint(obj, index):
        cw, ch, cd = obj['clearance_w'], obj['clearance_h'], obj['clearance_d']
        if index == 0:
            return cw, cd, ch
        if index == 1:
            return cd, cw, ch
        if index == 2:
            return cw, ch, cd
        return ch, cw, cd

    bay_records = []
    for bay in snap.bays:
        bay_records.append({
            'bay_id': bay['bay_id'],
            'interior_w': int(bay['interior_w']) * 1000,
            'interior_h': int(bay['interior_h']) * 1000,
            'interior_d': int(bay['interior_d']) * 1000,
            'lux_level': int(bay['lux_level']),
            'bay_priority': int(bay['bay_priority']),
            'blocked': blocked_classes.get(bay['bay_id'], set()),
        })
    bays_by_id = sorted(bay_records, key=lambda b: b['bay_id'])
    bays_by_scan = sorted(bay_records, key=lambda b: (b['bay_priority'], b['bay_id']))

    def fitting_orientation(obj, bay):
        """Smallest admissible orientation that fits, or None."""
        for index in obj['orientations']:
            fw, fd, fh = footprint(obj, index)
            if fw <= bay['interior_w'] and fd < bay['interior_d'] and fh <= bay['interior_h']:
                return index
        return None

    # ---- Stage 4: bay candidate matrix ------------------------------------
    bay_candidate_rows = []
    for obj in objects:
        for bay in bays_by_id:
            index = fitting_orientation(obj, bay)
            bay_candidate_rows.append({
                'object_id': obj['object_id'],
                'bay_id': bay['bay_id'],
                'fits': index is not None,
                'orientation': index if index is not None else -1,
            })

    # ---- Stage 5: selected placement (geometry only) ----------------------
    selected_rows = []
    rejected_rows = []
    for obj in objects:
        chosen = None
        for bay in bays_by_scan:
            index = fitting_orientation(obj, bay)
            if index is not None:
                chosen = (bay['bay_id'], index)
                break
        if chosen is None:
            code = 'P_NO_ORIENTATION' if obj['orientations'] == [0] else 'P_NO_FIT'
            obj['geometric_bay'] = None
            obj['geometric_orientation'] = -1
            obj['geometric_code'] = code
            rejected_rows.append({'object_id': obj['object_id'], 'code': code})
        else:
            obj['geometric_bay'] = chosen[0]
            obj['geometric_orientation'] = chosen[1]
            obj['geometric_code'] = 'P_OK'
            selected_rows.append({
                'object_id': obj['object_id'],
                'bay_id': chosen[0],
                'orientation': chosen[1],
                'code': 'P_OK',
            })

    # ---- Stage 6: conservation adjustment ---------------------------------
    conservation_rows = []
    for obj in objects:
        chosen = None
        blocked = False
        for bay in bays_by_scan:
            index = fitting_orientation(obj, bay)
            if index is None:
                continue
            blocked = True
            cons_class = obj['conservation_class']
            if bay['lux_level'] > obj['max_lux']:
                continue
            if cons_class in bay['blocked']:
                continue
            if needs_dark.get(cons_class, False) and bay['lux_level'] > 0:
                continue
            chosen = (bay['bay_id'], index)
            break
        if chosen is not None:
            obj['bay_id'] = chosen[0]
            obj['orientation'] = chosen[1]
            obj['conservation_code'] = 'P_OK'
            conservation_rows.append({
                'object_id': obj['object_id'],
                'bay_id': chosen[0],
                'orientation': chosen[1],
                'code': 'P_OK',
            })
        else:
            obj['bay_id'] = None
            obj['orientation'] = -1
            code = 'P_CONSERVATION_BLOCK' if blocked else obj['geometric_code']
            obj['conservation_code'] = code
            conservation_rows.append({
                'object_id': obj['object_id'],
                'bay_id': '',
                'orientation': -1,
                'code': code,
            })
    placed = [o for o in objects if o['bay_id'] is not None]

    # ---- Stage 7: crate candidates ----------------------------------------
    crate_records = {}
    for crate in snap.crates:
        inner = sorted([
            int(crate['inner_w']) * 1000,
            int(crate['inner_h']) * 1000,
            int(crate['inner_d']) * 1000,
        ])
        crate_records[crate['crate_class']] = {
            'crate_class': crate['crate_class'],
            'inner': inner,
            'interior_volume': inner[0] * inner[1] * inner[2],
            'catalog_count': int(crate['count']),
            'transport_mode': crate['transport_mode'],
        }
    crate_ids = sorted(crate_records)

    for obj in objects:
        fits = []
        for cid in crate_ids:
            record = crate_records[cid]
            if all(obj['handle_dims'][i] <= record['inner'][i] for i in range(3)):
                fits.append((record['interior_volume'] - obj['handling_volume'], cid))
        obj['crate_fits'] = fits
        obj['primary_crate'] = sorted(fits)[0][1] if fits else None

    crate_candidate_rows = []
    for oid in sorted(by_id):
        fits = sorted(by_id[oid]['crate_fits'])
        for residual, cid in fits:
            crate_candidate_rows.append({
                'object_id': oid,
                'crate_class': cid,
                'residual': residual,
            })

    # ---- Stage 10: dependency edges ---------------------------------------
    edges = {}
    for rule in snap.dependency_rules:
        key = (rule['from_object'], rule['to_object'])
        if key[0] not in by_id or key[1] not in by_id:
            continue
        edges[key] = {
            'from': key[0],
            'to': key[1],
            'kind': 'D_RULE',
            'weight': int(rule['edge_priority']),
        }

    by_bay = {}
    for obj in placed:
        by_bay.setdefault(obj['bay_id'], []).append(obj)

    for bay_id in sorted(by_bay):
        members = by_bay[bay_id]
        for earlier in members:
            for later in members:
                if earlier['object_id'] == later['object_id']:
                    continue
                ekey = (earlier['object_id'], later['object_id'])
                if earlier['handling_volume'] > later['handling_volume']:
                    candidate = {
                        'from': ekey[0],
                        'to': ekey[1],
                        'kind': 'D_BAYSIZE',
                        'weight': earlier['handling_volume'],
                    }
                    if ekey not in edges or KIND_RANK[edges[ekey]['kind']] < KIND_RANK['D_BAYSIZE']:
                        edges[ekey] = candidate
                if sensitivity(earlier['conservation_class']) < sensitivity(later['conservation_class']):
                    candidate = {
                        'from': ekey[0],
                        'to': ekey[1],
                        'kind': 'D_CONSERVATION',
                        'weight': earlier['handling_volume'],
                    }
                    if ekey not in edges or KIND_RANK[edges[ekey]['kind']] < KIND_RANK['D_CONSERVATION']:
                        edges[ekey] = candidate

    edge_list = sorted(edges.values(), key=lambda e: (e['from'], e['to']))

    # ---- Stage 11: cycle resolution ---------------------------------------
    def find_cycle(adjacency):
        """First cycle found by depth first search from the lowest node id."""
        state = {node: 0 for node in adjacency}
        for start in sorted(adjacency):
            if state[start] != 0:
                continue
            path = [start]
            frames = [(start, iter(sorted(adjacency[start])))]
            state[start] = 1
            while frames:
                node, iterator = frames[-1]
                nxt = next(iterator, None)
                if nxt is None:
                    state[node] = 2
                    path.pop()
                    frames.pop()
                    continue
                if state[nxt] == 0:
                    state[nxt] = 1
                    path.append(nxt)
                    frames.append((nxt, iter(sorted(adjacency[nxt]))))
                elif state[nxt] == 1:
                    return path[path.index(nxt):]
        return None

    def count_independent_cycles(adjacency):
        """Number of vertex sets still strongly connected beyond one node."""
        index = {}
        low = {}
        stack = []
        on_stack = set()
        counter = [0]
        found = [0]

        for root in sorted(adjacency):
            if root in index:
                continue
            work = [(root, iter(sorted(adjacency[root])))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, iterator = work[-1]
                advanced = False
                for nxt in iterator:
                    if nxt not in adjacency:
                        continue
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter[0]
                        counter[0] += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(sorted(adjacency[nxt]))))
                        advanced = True
                        break
                    elif nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        found[0] += 1
        return found[0]

    adjacency = {oid: set() for oid in object_ids}
    for edge in edge_list:
        if edge['from'] in adjacency and edge['to'] in adjacency:
            adjacency[edge['from']].add(edge['to'])

    removed_edges = []
    converged = False
    guard = len(edge_list) + 1
    while guard > 0:
        guard -= 1
        cycle = find_cycle(adjacency)
        if cycle is None:
            converged = True
            break
        cycle_edges = []
        for i in range(len(cycle)):
            src = cycle[i]
            dst = cycle[(i + 1) % len(cycle)]
            cycle_edges.append(edges[(src, dst)])
        victim = min(cycle_edges, key=lambda e: (
            KIND_RANK[e['kind']], e['weight'], e['from'], e['to']))
        adjacency[victim['from']].discard(victim['to'])
        removed_edges.append(victim)

    final_cycle_count = count_independent_cycles(adjacency) if converged else 1

    # ---- Stage 12: install order ------------------------------------------
    incoming = {oid: set() for oid in object_ids}
    for edge in edge_list:
        if edge['from'] in adjacency and edge['to'] in adjacency[edge['from']]:
            incoming[edge['to']].add(edge['from'])

    install_order = []
    installed = set()
    remaining = set(object_ids)
    while remaining:
        ready = [oid for oid in remaining if not (incoming[oid] - installed)]
        if not ready:
            ready = sorted(remaining, key=lambda oid: (by_id[oid]['setup_seconds'], oid))
        choice = min(ready, key=lambda oid: (by_id[oid]['setup_seconds'], oid))
        install_order.append(choice)
        installed.add(choice)
        remaining.discard(choice)
    position_of = {oid: i + 1 for i, oid in enumerate(install_order)}

    # ---- Stage 8: shared crate capacity -----------------------------------
    available = {}
    prior_consumed = {}
    for cid in crate_ids:
        available[cid] = crate_records[cid]['catalog_count']
        prior_consumed[cid] = 0
    for row in snap.prior_state:
        cid = row['crate_class']
        if cid in available:
            prior_consumed[cid] += int(row['consumed_count'])
    for cid in crate_ids:
        available[cid] = crate_records[cid]['catalog_count'] - prior_consumed[cid]

    reservations = {}
    for row in snap.reservations:
        key = (row['crate_class'], row['lender_id'])
        reservations[key] = reservations.get(key, 0) + int(row['reserved_count'])

    for obj in objects:
        obj['crate_class'] = None
        obj['crate_code'] = None

    assignment = {}
    for oid in install_order:
        obj = by_id[oid]
        if obj['bay_id'] is None:
            continue
        candidates = []
        for residual, cid in obj['crate_fits']:
            if available.get(cid, 0) > 0:
                candidates.append((cid, residual))
        if not candidates:
            obj['crate_class'] = None
            obj['crate_code'] = 'C_NO_CRATE'
            continue
        def rank(item):
            cid, residual = item
            reserved = 1 if reservations.get((cid, obj['lender_id']), 0) > 0 else 0
            return (0 if reserved else 1, residual, cid)
        chosen_class, _ = min(candidates, key=rank)
        obj['crate_class'] = chosen_class
        obj['crate_code'] = 'C_PRIMARY' if chosen_class == obj['primary_crate'] else 'C_DISPLACED'
        available[chosen_class] -= 1
        key = (chosen_class, obj['lender_id'])
        if reservations.get(key, 0) > 0:
            reservations[key] -= 1
        assignment[oid] = chosen_class

    crate_assignment_rows = []
    for oid in sorted(by_id):
        obj = by_id[oid]
        if obj['bay_id'] is None:
            continue
        crate_assignment_rows.append({
            'object_id': oid,
            'crate_class': obj['crate_class'] if obj['crate_class'] else '',
            'code': obj['crate_code'],
            'primary_class': obj['primary_crate'] if obj['primary_crate'] else '',
        })

    capacity_rows = []
    for cid in crate_ids:
        capacity_rows.append({
            'crate_class': cid,
            'catalog_count': crate_records[cid]['catalog_count'],
            'prior_consumed': prior_consumed[cid],
            'remaining': available[cid],
        })

    contention_rows = []
    for oid in sorted(by_id):
        obj = by_id[oid]
        if obj.get('crate_code') != 'C_DISPLACED':
            continue
        contention_rows.append({
            'object_id': oid,
            'wanted_class': obj['primary_crate'] if obj['primary_crate'] else '',
            'assigned_class': obj['crate_class'] if obj['crate_class'] else '',
        })

    # ---- Stage 9: transport legs ------------------------------------------
    transport_rows = []
    for oid in sorted(by_id):
        obj = by_id[oid]
        if obj['bay_id'] is None or obj['crate_class'] is None:
            continue
        mode = crate_records[obj['crate_class']]['transport_mode']
        exception = False
        rule = snap.transport.get(obj['lender_id'])
        if rule is not None and rule['forbidden_mode'] == mode:
            mode = rule['fallback_mode']
            exception = True
        transport_rows.append({
            'object_id': oid,
            'crate_class': obj['crate_class'],
            'mode': mode,
            'exception': exception,
        })

    # ---- Stage 13: crew, setup and windows --------------------------------
    crew_rows = []
    for obj in objects:
        crew_rows.append({
            'object_id': obj['object_id'],
            'crew_class': obj['crew_class'],
            'setup_seconds': obj['setup_seconds'],
        })

    window_rows = []
    unschedulable_rows = []
    for obj in objects:
        required_days = ceil(ratio(obj['setup_seconds'], SECONDS_PER_DAY)) if SECONDS_PER_DAY else 0
        obj['required_days'] = required_days
        obj['scheduled'] = False
        if obj['bay_id'] is None:
            code = 'S_NO_PLACEMENT'
            start = end = 0
        elif obj['crate_class'] is None:
            code = 'S_NO_CRATE'
            start, end = obj['window_start'], obj['window_end'] = compute_window(
                obj, snap, venue_end_exclusive, cons_end_inclusive, needs_dark)
        else:
            start, end = compute_window(
                obj, snap, venue_end_exclusive, cons_end_inclusive, needs_dark)
            obj['window_start'], obj['window_end'] = start, end
            days_available = end - start + 1
            if days_available <= 0:
                code = 'S_NO_WINDOW'
            elif required_days > days_available:
                code = 'S_SHORT_WINDOW'
            else:
                code = 'S_OK'
                obj['scheduled'] = True
        obj['window_code'] = code
        window_rows.append({
            'object_id': obj['object_id'],
            'bay_id': obj['bay_id'] if obj['bay_id'] else '',
            'window_start': start,
            'window_end': end,
            'required_days': required_days,
            'code': code,
        })
        if code != 'S_OK':
            unschedulable_rows.append({'object_id': obj['object_id'], 'code': code})
    unschedulable_rows.sort(key=lambda r: (r['object_id'], r['code']))

    # ---- Stage 14: dossier ------------------------------------------------
    occupancy_rows = []
    for bay in bays_by_id:
        members = sorted(o['object_id'] for o in placed if o['bay_id'] == bay['bay_id'])
        occupancy_rows.append({
            'bay_id': bay['bay_id'],
            'object_count': len(members),
            'objects': members,
        })

    def value_class_row(obj):
        class_by_min = {int(r['min_value']): r for r in snap.value_classes}
        declared = obj['declared_value_cents'] // 100
        eligible = [m for m in class_by_min if declared >= m]
        if not eligible:
            return {'unschedulable_penalty': 0, 'contention_penalty': 0}
        return class_by_min[max(eligible)]

    unschedulable_penalty = 0
    contention_penalty = 0
    for obj in objects:
        row = value_class_row(obj)
        if not obj['scheduled']:
            unschedulable_penalty += int(row['unschedulable_penalty'])
        if obj.get('crate_code') == 'C_DISPLACED':
            contention_penalty += int(row['contention_penalty'])

    displacement_count = sum(1 for o in objects if o.get('crate_code') == 'C_DISPLACED')
    total_residual = 0
    for obj in objects:
        if obj['crate_class'] is not None:
            for residual, cid in obj['crate_fits']:
                if cid == obj['crate_class']:
                    total_residual += residual
                    break

    total_setup_seconds = sum(o['setup_seconds'] for o in objects)
    total_insurance_cents = 0
    for obj in objects:
        lender = snap.lenders.get(obj['lender_id'])
        if lender is None or int(lender['insurance_rate_den']) == 0:
            premium = 0
        else:
            premium = round_half_up(
                obj['declared_value_cents'] * int(lender['insurance_rate_num']),
                int(lender['insurance_rate_den']))
        obj['insurance_cents'] = premium
        total_insurance_cents += premium

    audit_rows = []
    for obj in objects:
        audit_rows.append({
            'object_id': obj['object_id'],
            'placed_bay': obj['bay_id'] if obj['bay_id'] else '',
            'crate_class': obj['crate_class'] if obj['crate_class'] else '',
            'displaced': obj.get('crate_code') == 'C_DISPLACED',
            'scheduled': obj['scheduled'],
            'install_position': position_of[obj['object_id']],
            'setup_seconds': obj['setup_seconds'],
            'insurance_cents': obj['insurance_cents'],
        })

    placed_objects = sum(1 for o in objects if o['scheduled'])
    objective = [
        placed_objects,
        unschedulable_penalty,
        contention_penalty,
        len(removed_edges),
        total_residual,
    ]
    order_names = list(snap.objective_order['order'])
    senses = list(snap.objective_order['sense'])

    penalty_summary = {
        'unschedulable_penalty': unschedulable_penalty,
        'contention_penalty': contention_penalty,
        'cycles_broken': len(removed_edges),
        'total_insurance_cents': total_insurance_cents,
    }
    objective_tuple = {'objective': objective, 'order': order_names}

    artifacts = {
        'canonical_objects.jsonl': [
            {'object_id': o['object_id'], 'lender_id': o['lender_id'],
             'material_code': o['material_code'], 'version': o['version'],
             'declared_value_cents': o['declared_value_cents'], 'mass_g': o['mass_g']}
            for o in objects],
        'normalized_geometry.jsonl': [
            {'object_id': o['object_id'], 'clearance_w': o['clearance_w'],
             'clearance_h': o['clearance_h'], 'clearance_d': o['clearance_d'],
             'aspect_num': o['aspect_num'], 'aspect_den': o['aspect_den']}
            for o in objects],
        'admissibility_matrix.jsonl': orientation_rows,
        'bay_candidate_matrix.jsonl': bay_candidate_rows,
        'selected_placement.jsonl': selected_rows,
        'rejected_placement.jsonl': sorted(rejected_rows, key=lambda r: (r['object_id'], r['code'])),
        'conservation_adjusted.jsonl': conservation_rows,
        'crate_candidate_matrix.jsonl': crate_candidate_rows,
        'crate_assignment.jsonl': crate_assignment_rows,
        'crate_capacity_ledger.jsonl': capacity_rows,
        'crate_contention_report.jsonl': contention_rows,
        'transport_leg_manifest.jsonl': transport_rows,
        'dependency_edges.jsonl': edge_list,
        'cycle_resolution_audit.jsonl': removed_edges,
        'install_order.jsonl': [
            {'position': i + 1, 'object_id': oid} for i, oid in enumerate(install_order)],
        'crew_requirement_ledger.jsonl': crew_rows,
        'install_window_table.jsonl': window_rows,
        'unschedulable_report.jsonl': unschedulable_rows,
        'gallery_occupancy_map.jsonl': occupancy_rows,
        'deterministic_audit.jsonl': audit_rows,
    }

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    for name, rows in artifacts.items():
        path = os.path.join(out_dir, name)
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, separators=(',', ':')))
                handle.write('\n')

    write_json(out_dir, 'penalty_summary.json', penalty_summary)
    write_json(out_dir, 'objective_tuple.json', objective_tuple)

    program_path = os.path.abspath(__file__)
    with open(program_path, 'rb') as handle:
        program_digest = hashlib.sha256(handle.read()).hexdigest()
    manifest = {}
    for name in artifacts:
        manifest[name] = sha256_file(os.path.join(out_dir, name))
    manifest['penalty_summary.json'] = sha256_file(os.path.join(out_dir, 'penalty_summary.json'))
    manifest['objective_tuple.json'] = sha256_file(os.path.join(out_dir, 'objective_tuple.json'))
    # The program is hashed under its fixed artifact name so the manifest does not
    # depend on where the program file itself lives.
    manifest['plan.py'] = program_digest
    write_json(out_dir, 'integrity_manifest.json', manifest)

    chain = str(snap.horizon.get('chain_seed', '0' * 64))
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode('utf-8')).hexdigest()

    final_state = {
        'chain': chain,
        'repair_converged': converged,
        'cycle_edges_removed': len(removed_edges),
        'final_cycle_count': final_cycle_count,
        'displacement_count': displacement_count,
        'unschedulable_count': len(unschedulable_rows),
        'placed_objects': placed_objects,
        'total_setup_seconds': total_setup_seconds,
        'total_insurance_cents': total_insurance_cents,
        'objective': objective,
    }
    write_json(out_dir, 'final_state.json', final_state)
    return 0


def compute_window(obj, snap, venue_end_exclusive, cons_end_inclusive, needs_dark):
    cons = snap.conservation.get(obj['conservation_class'])
    venue = snap.venue_windows.get(obj['bay_id'])
    crew = snap.crew.get(obj['crew_class'])
    starts = []
    ends = []
    if cons is not None:
        starts.append(int(cons['setup_start']))
        end = int(cons['setup_end'])
        if not cons_end_inclusive:
            end -= 1
        ends.append(end)
    if venue is not None:
        starts.append(int(venue['access_start']))
        end = int(venue['access_end'])
        if venue_end_exclusive:
            end -= 1
        ends.append(end)
    if crew is not None:
        starts.append(int(crew['avail_start']))
        ends.append(int(crew['avail_end']))
    if not starts:
        return 0, 0
    return max(starts), min(ends)


def write_json(out_dir, name, value):
    path = os.path.join(out_dir, name)
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2))
        handle.write('\n')


def sha256_file(path):
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
