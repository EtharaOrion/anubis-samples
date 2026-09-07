#!/usr/bin/env python3
"""Derive the season settlement for the tactics league from sealed replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Everything the material states is parsed from the documents in DATA_DIR rather
than hard coded: the published rosters, the policy scalars, the control figures
and the guard bit table.  The three unstated quantities and the two unstated
season policy figures are pinned by searching their published rosters against
the control figures.  Nine reports are written into OUT_DIR.

Standard library only; nothing is installed, no network is reached, and no file
outside DATA_DIR is read.
"""

import csv
import hashlib
import json
import os
import re
import struct
import sys
from collections import Counter
from itertools import combinations
from math import gcd

MASK32 = 0xFFFFFFFF

# kind ledger names (reconciliation-invariants.md)
KIND_NAMES = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
              5: 'concede', 6: 'transfer', 8: 'close'}
LEDGER_ORDER = ['deploy', 'output', 'eliminate', 'disband', 'concede',
                'transfer', 'close']

GUARD_ORDER = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
               'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']

# the control figures, in the order calibration-controls.md publishes them
FIGURE_KEYS = ['segments', 'records', 'squads', 'gross', 'voided', 'raw_upkeep',
               'charged', 'overflow', 'champion', 'qualifier', 'provisional']
TIER_NAMES = ('champion', 'contender', 'qualifier', 'provisional')


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def read_text(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def parse_roster(text, label):
    """`Label: `a`, `b`, `c` -> ['a','b','c'] (ints when every item is numeric)."""
    m = re.search(re.escape(label) + r'\s*:\s*(.+)', text)
    if not m:
        raise ValueError('roster line %r not found' % label)
    items = [x.strip().strip('`').strip() for x in m.group(1).strip().split(',')]
    items = [x for x in items if x != '']
    if not items:
        raise ValueError('roster line %r is empty' % label)
    if all(re.fullmatch(r'-?\d+', x) for x in items):
        return [int(x) for x in items]
    return items


def parse_scalar(text, label):
    vals = parse_roster(text, label)
    if len(vals) != 1:
        raise ValueError('scalar %r: expected one value, got %r' % (label, vals))
    return vals[0]


def parse_guard_table(text):
    """| bit | guard | -> {guard: bit}."""
    table = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) >= 2 and re.fullmatch(r'\d+', cells[0]) and cells[1] in GUARD_ORDER:
            table[cells[1]] = int(cells[0])
    if not table:
        table = {name: i for i, name in enumerate(GUARD_ORDER)}
    return table


def parse_control_figures(text):
    """- C1 matches opened: 5 -> {'C1': ('matches opened', 5)}."""
    out = {}
    for m in re.finditer(r'^\s*-\s*(C\d+)\s+(.+?)\s*:\s*(-?\d+)\s*$', text, re.M):
        out[m.group(1)] = (m.group(2), int(m.group(3)))
    if not out:
        raise ValueError('no control figures found')
    return out


def load_csv(path):
    with open(path, 'r', encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


class Material(object):
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.texts = {}
        for name in sorted(os.listdir(data_dir)):
            full = os.path.join(data_dir, name)
            if os.path.isfile(full) and name.endswith('.md'):
                self.texts[name] = read_text(full)

        guards_md = self._doc('replay-guards')
        self.guard_bits = parse_guard_table(guards_md)
        self.engine_tags = parse_roster(guards_md, 'Engine tag roster')
        self.window_depths = parse_roster(guards_md, 'Window depth roster')
        self.sentinel_roster = parse_roster(guards_md, 'Sentinel roster')
        self.sentinels = []
        for value in self.sentinel_roster:
            self.sentinels.append(int(str(value), 16) if isinstance(value, str)
                                  else value)

        self.thresholds = parse_roster(self._doc('standings-policy'),
                                       'Promotion threshold roster')
        upkeep_md = self._doc('upkeep-policy')
        self.budgets = parse_roster(upkeep_md, 'Budget cap roster')
        bracket_md = self._doc('bracket-policy')
        self.eligibility_floor = parse_scalar(bracket_md, 'Eligibility match floor')
        self.prominence = parse_scalar(bracket_md, 'Ladder weight prominence')
        self.standing_floor = parse_scalar(bracket_md, 'Standing points floor')
        self.weight_floor = parse_scalar(bracket_md, 'Combined ladder weight floor')
        self.gross_floor = parse_scalar(bracket_md, 'Combined gross output floor')
        self.review_weight = parse_scalar(bracket_md, 'Review weight')
        self.docket_size = parse_scalar(bracket_md, 'Docket size')
        self.docket_cap = parse_scalar(bracket_md, 'Docket conference cap')

        self.controls = parse_control_figures(self._doc('calibration-controls'))
        self.control_labels = sorted(self.controls, key=lambda c: int(c[1:]))
        if len(self.control_labels) != len(FIGURE_KEYS):
            raise ValueError('expected %d control figures, found %d'
                             % (len(FIGURE_KEYS), len(self.control_labels)))
        # the published figures map onto FIGURE_KEYS in published order
        self.figure_label = dict(zip(FIGURE_KEYS, self.control_labels))

        self.registry = {}
        for row in load_csv(os.path.join(data_dir, 'entrant-registry.csv')):
            self.registry[row['squad_key']] = dict(
                entrant_id=row['entrant_id'], squad_key=row['squad_key'],
                division=row['division'], licence_class=row['licence_class'],
                is_ranked=row['is_ranked'].strip().lower() == 'yes')
        self.divisions = {}
        for row in load_csv(os.path.join(data_dir, 'division-topology.csv')):
            self.divisions[row['division']] = dict(
                division=row['division'], conference=row['conference'],
                ladder_weight=int(row['ladder_weight']))
        self.licences = {}
        for row in load_csv(os.path.join(data_dir, 'licence-classes.csv')):
            self.licences[row['licence_class']] = dict(
                licence_class=row['licence_class'], segment=row['segment'],
                levy_rate=int(row['levy_rate']))

        self.conferences = []
        for d in self.divisions.values():
            if d['conference'] not in self.conferences:
                self.conferences.append(d['conference'])
        self.segments = []
        for l in self.licences.values():
            if l['segment'] not in self.segments:
                self.segments.append(l['segment'])

        self.captures = {}
        self.transcript = None
        for name in sorted(os.listdir(data_dir)):
            full = os.path.join(data_dir, name)
            if not os.path.isfile(full):
                continue
            with open(full, 'rb') as fh:
                if fh.read(4) == b'RJNL':
                    self.captures[name] = full
                else:
                    fh.seek(0)
                    if name.endswith('.jsonl'):
                        self.transcript = full

    def _doc(self, stem):
        for name, text in self.texts.items():
            if name.startswith(stem):
                return text
        raise ValueError('document %r not found' % stem)

    def meta(self, key):
        row = self.registry.get(key)
        if row is None:
            return dict(registered=False, squad_key=key, entrant_id=None,
                        division=None, conference=None, ladder_weight=1,
                        licence_class=None, segment=None, levy_rate=1,
                        is_ranked=False)
        div = self.divisions[row['division']]
        lic = self.licences[row['licence_class']]
        return dict(registered=True, squad_key=key, entrant_id=row['entrant_id'],
                    division=row['division'], conference=div['conference'],
                    ladder_weight=div['ladder_weight'],
                    licence_class=row['licence_class'], segment=lic['segment'],
                    levy_rate=lic['levy_rate'], is_ranked=row['is_ranked'])


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

class DecodeError(Exception):
    pass


def rot_right(buf, k):
    w = len(buf)
    k %= w
    return buf[w - k:] + buf[:w - k]


def parse_capture(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw[0:4] != b'RJNL':
        raise DecodeError('bad envelope marker')
    checksum = struct.unpack('<I', raw[32:36])[0]
    env = dict(
        version=raw[4], reserved=raw[5],
        guard_register=struct.unpack('<H', raw[6:8])[0],
        nonce_raw=raw[8:12],
        declared_segment_count=struct.unpack('<I', raw[12:16])[0],
        declared_record_count=struct.unpack('<I', raw[16:20])[0],
        base_tick=struct.unpack('<I', raw[20:24])[0],
        capture_id=struct.unpack('<I', raw[24:28])[0],
        declared_roster_size=struct.unpack('<I', raw[28:32])[0],
        envelope_checksum=checksum,
        envelope_checksum_agrees=(sum(raw[:32]) & MASK32) == checksum)
    env['guards'] = [name for i, name in enumerate(GUARD_ORDER)
                     if env['guard_register'] >> i & 1]
    segments = []
    off = 36
    while off + 28 <= len(raw):
        if raw[off:off + 4] != b'SEG0':
            raise DecodeError('bad segment marker at offset %d' % off)
        slot, match_id, count, salt, width, pay_checksum = struct.unpack(
            '<6I', raw[off + 4:off + 28])
        payload = raw[off + 28:off + 28 + width]
        if len(payload) != width:
            raise DecodeError('truncated segment payload')
        segments.append(dict(
            storage_slot=slot, match_id=match_id, declared_record_count=count,
            written_salt=salt, payload_width=width,
            declared_payload_checksum=pay_checksum,
            payload_checksum_agrees=(sum(payload) & MASK32) == pay_checksum,
            payload=payload))
        off += 28 + width
    return env, segments


def segperm_step(nonce, n):
    base = (nonce >> 8) % n
    cand = base + 2
    while gcd(cand, n) != 1:
        cand += 1
    return cand


def decode_capture(path, engine_tag, window_depth, sentinel, dictionary, guard_bits):
    env, segments = parse_capture(path)

    def on(name):
        return bool(env['guard_register'] >> guard_bits[name] & 1)

    bias = sum(engine_tag.encode('utf-8')) % 11 if on('countbias') else 0
    nonce = struct.unpack('>I' if on('endian') else '<I', env['nonce_raw'])[0]
    n = len(segments)
    for seg in segments:
        seg['record_count'] = seg['declared_record_count'] - bias
        if seg['record_count'] < 0:
            raise DecodeError('segment declares fewer records than the count bias')

    order = list(range(n))
    step = None
    if on('segperm') and n > 1:
        step = segperm_step(nonce, n)
        by_logical = {}
        for i in range(n):
            by_logical[(nonce + i * step) % n] = i
        if len(by_logical) != n:
            raise DecodeError('segment permutation step is not coprime')
        order = [by_logical[L] for L in range(n)]

    window = []
    records = []
    notes = Counter()
    prev_tick = nonce
    running_value_sum = 0

    for logical, idx in enumerate(order):
        seg = segments[idx]
        if on('driftkey') and logical > 0:
            eff_salt = (seg['written_salt'] ^ running_value_sum) & MASK32
        else:
            eff_salt = seg['written_salt']
        seg['logical_position'] = logical
        seg['effective_salt'] = eff_salt
        payload = seg['payload']
        low = eff_salt & 0xFF
        pos = 0
        first_diff = 0
        prev_tick_in_segment = 0
        for ordinal_segment in range(seg['record_count']):
            if pos + 2 > len(payload):
                raise DecodeError('segment payload overrun')
            visible, shadow = payload[pos], payload[pos + 1]
            width = (shadow ^ low) if on('shadow') else visible
            if width <= 0 or pos + 2 + width > len(payload):
                raise DecodeError('unreadable frame width %d' % width)
            blob = payload[pos + 2:pos + 2 + width]
            ordinal_capture = len(records)

            if on('keystream'):
                digest = hashlib.sha256(('%s:%d:%d:%d' % (
                    engine_tag, seg['match_id'], eff_salt, ordinal_segment))
                    .encode('ascii')).digest()
                blob = bytes(a ^ b for a, b in zip(blob, digest))
            if on('rotate'):
                blob = rot_right(blob, (eff_salt + ordinal_capture) % width)

            directives = []
            head = 0
            if on('escape'):
                while (head + 1 < len(blob) and blob[head] == sentinel
                       and len(directives) < 2):
                    directives.append(blob[head + 1])
                    head += 2
            body = blob[head:]
            if len(body) < 12:
                raise DecodeError('record body too short')
            kind, mode = body[0], body[1]
            if mode == 0:
                if len(body) != 15:
                    raise DecodeError('mode 0 body width %d' % len(body))
                attr_field, fixed = body[2:5].decode('ascii'), 5
            elif mode in (1, 2):
                if len(body) != 13:
                    raise DecodeError('mode %d body width %d' % (mode, len(body)))
                attr_field, fixed = body[2], 3
            else:
                raise DecodeError('attribution mode %d' % mode)
            unit = struct.unpack('<H', body[fixed:fixed + 2])[0]
            tick_field = struct.unpack('<I', body[fixed + 2:fixed + 6])[0]
            value_field = struct.unpack('<I', body[fixed + 6:fixed + 10])[0]

            if on('delta2'):
                first_diff = (first_diff + tick_field) & MASK32
                tick = (prev_tick_in_segment + first_diff) & MASK32
                prev_tick_in_segment = tick
            else:
                tick = tick_field
            value = (value_field ^ prev_tick) if on('chain') else value_field
            prev_tick = tick

            if mode == 0:
                wire_key = attr_field
            elif mode == 1:
                if not on('backref'):
                    raise DecodeError('back reference with the guard idle')
                if attr_field >= len(window):
                    raise DecodeError('back reference %d beyond a window of %d'
                                      % (attr_field, len(window)))
                wire_key = window[attr_field]
            else:
                if not on('alias'):
                    raise DecodeError('dictionary position with the guard idle')
                if attr_field >= len(dictionary):
                    raise DecodeError('dictionary position %d beyond %d'
                                      % (attr_field, len(dictionary)))
                wire_key = dictionary[attr_field]

            # the window refreshes on the key as written on the wire, before any
            # escape directive is weighed
            if wire_key in window:
                window.remove(wire_key)
            window.append(wire_key)
            if len(window) > window_depth:
                window = window[len(window) - window_depth:]

            owner = wire_key
            for directive in directives:
                op, operand = directive >> 4, directive & 0xF
                if op == 1:
                    if operand >= len(dictionary):
                        raise DecodeError('redirect to dictionary position %d' % operand)
                    owner = dictionary[operand]
                elif op == 2:
                    if owner not in dictionary:
                        # the record holds no dictionary position, so the advance
                        # cannot move it; counted, never silenced
                        notes['escape advance on a key outside the dictionary'] += 1
                    else:
                        owner = dictionary[(dictionary.index(owner) + operand)
                                           % len(dictionary)]
                else:
                    raise DecodeError('escape operation %d' % op)

            records.append(dict(
                kind=kind, mode=mode, wire_key=wire_key, owner=owner, unit=unit,
                tick=tick, value=value, directives=list(directives),
                record_ordinal=ordinal_capture, segment_ordinal=ordinal_segment,
                logical_segment=logical, match_id=seg['match_id'],
                wire_width=width, effective_salt=eff_salt))
            running_value_sum = (running_value_sum + value) & MASK32
            pos += 2 + width

        seg['filler_width'] = len(payload) - pos
        seg['filler_width_matches_written_salt'] = (
            seg['filler_width'] == 3 + seg['written_salt'] % 5)
        seg['filler_width_matches_effective_salt'] = (
            seg['filler_width'] == 3 + eff_salt % 5)
        ticks = [r['tick'] for r in records if r['logical_segment'] == logical]
        seg['tick_backsteps'] = sum(1 for a, b in zip(ticks, ticks[1:]) if b < a)

    return dict(path=path, env=env, segments=segments, order=order,
                records=records, nonce=nonce, count_bias=bias,
                segperm_step=step, notes=dict(notes))


# --------------------------------------------------------------------------
# ownership settlement
# --------------------------------------------------------------------------

def settle_ownership(squads, handover):
    """Effective owner of every squad and the number of sweeps taken."""
    resolved = {}
    on_cycle = {}
    for start in handover:
        if start in resolved:
            continue
        path, seen, cur = [], {}, start
        while True:
            if cur in resolved:
                for member in path:
                    resolved[member] = resolved[cur]
                break
            if cur in seen:
                # a closed set settles on the member whose key sorts first
                cycle = path[seen[cur]:]
                chosen = min(cycle)
                for member in cycle:
                    resolved[member] = chosen
                    on_cycle[member] = chosen
                for member in path[:seen[cur]]:
                    resolved[member] = chosen
                break
            if cur not in handover:
                for member in path:
                    resolved[member] = cur
                break
            seen[cur] = len(path)
            path.append(cur)
            cur = handover[cur]

    # a sweep hands every squad to the squad its current owner hands to; a
    # closed set hands to its chosen member
    step = {}
    for s in squads:
        if s in on_cycle:
            step[s] = on_cycle[s]
        elif s in handover:
            step[s] = handover[s]
        else:
            step[s] = s
    owner = {s: s for s in squads}
    passes = 0
    while True:
        passes += 1
        nxt = {s: step[owner[s]] for s in squads}
        if nxt == owner:
            break
        owner = nxt
    return owner, passes


# --------------------------------------------------------------------------
# match settlement (figures are settled on the squad that recorded the events)
# --------------------------------------------------------------------------

def match_figures(records, close_tick):
    squads = []
    for rec in records:
        if rec['owner'] not in squads:
            squads.append(rec['owner'])
    state = {}
    for key in squads:
        state[key] = dict(gross_output=0, voided_output=0, raw_upkeep=0,
                          eliminated={}, disbanded={}, conceded=None)
    for rec in records:
        key, tick = rec['owner'], rec['tick']
        slot = (key, rec['unit'])
        if rec['kind'] == 3:
            if state[key]['eliminated'].get(slot) is None:
                state[key]['eliminated'][slot] = tick
        elif rec['kind'] == 4:
            if state[key]['disbanded'].get(slot) is None:
                state[key]['disbanded'][slot] = tick
        elif rec['kind'] == 5:
            if state[key]['conceded'] is None:
                state[key]['conceded'] = tick

    deployed = {}
    for rec in records:
        if rec['kind'] == 1:
            deployed[(rec['owner'], rec['unit'])] = (rec['tick'], rec['value'])

    for rec in records:
        if rec['kind'] != 2:
            continue
        key = rec['owner']
        slot = (key, rec['unit'])
        voided = False
        elim = state[key]['eliminated'].get(slot)
        if elim is not None and rec['tick'] >= elim:
            voided = True
        conceded = state[key]['conceded']
        if conceded is not None and rec['tick'] >= conceded:
            voided = True
        if voided:
            state[key]['voided_output'] += rec['value']
        else:
            state[key]['gross_output'] += rec['value']

    for (key, _unit), (start, rate) in deployed.items():
        end = close_tick
        disband = state[key]['disbanded'].get((key, _unit))
        if disband is not None:
            end = min(end, disband)
        if end is None or end < start:
            continue
        state[key]['raw_upkeep'] += rate * (end - start)

    conceding = set(k for k, v in state.items() if v['conceded'] is not None)
    return state, conceding


def tier_of(score, threshold):
    if score >= 3 * threshold:
        return 'champion'
    if score >= 2 * threshold:
        return 'contender'
    if score >= threshold:
        return 'qualifier'
    return 'provisional'


def ownership_of(records, dictionary):
    """Hand over map and effective owners for one capture."""
    handover = {}
    for rec in records:
        if rec['kind'] == 6:
            handover[rec['owner']] = dictionary[rec['value'] % len(dictionary)]
    seen = []
    for rec in records:
        if rec['owner'] not in seen:
            seen.append(rec['owner'])
    owner, passes = settle_ownership(seen, handover)
    return handover, owner, passes, seen


def control_reading(mat, path, engine_tag, depth, sentinel, dictionary,
                    budget=None, threshold=None, reattribute=False):
    """Reproduce every published figure for the control slice."""
    dec = decode_capture(path, engine_tag, depth, sentinel, dictionary, mat.guard_bits)
    env = dec['env']
    _handover, owner, _passes, _seen = ownership_of(dec['records'], dictionary)
    per_squad = {}
    for logical in sorted(set(r['logical_segment'] for r in dec['records'])):
        recs = [r for r in dec['records'] if r['logical_segment'] == logical]
        close = max([r['tick'] for r in recs if r['kind'] == 8], default=None)
        figs, _ = match_figures(recs, close)
        for key, fig in figs.items():
            target = owner[key] if reattribute else key
            acc = per_squad.setdefault(target, dict(gross=0, voided=0, raw=0, rows=[]))
            acc['gross'] += fig['gross_output']
            acc['voided'] += fig['voided_output']
            acc['raw'] += fig['raw_upkeep']
            acc['rows'].append((logical, fig['raw_upkeep']))

    charged_total = overflow_total = 0
    scores = {}
    for key, acc in per_squad.items():
        md = mat.meta(key)
        left = budget if budget is not None else 1 << 62
        charged = 0
        for _logical, raw in sorted(acc['rows']):
            take = min(raw, left)
            charged += take
            left -= take
        charged_total += charged
        overflow_total += acc['raw'] - charged
        scores[key] = acc['gross'] * md['ladder_weight'] - charged * md['levy_rate']

    tiers = Counter(tier_of(scores[k], threshold) for k in per_squad) \
        if threshold is not None else Counter()
    return dict(
        segments=env['declared_segment_count'] - dec['count_bias'],
        records=len(dec['records']),
        squads=len(per_squad),
        gross=sum(v['gross'] for v in per_squad.values()),
        voided=sum(v['voided'] for v in per_squad.values()),
        raw_upkeep=sum(v['raw'] for v in per_squad.values()),
        charged=charged_total,
        overflow=overflow_total,
        champion=tiers['champion'], qualifier=tiers['qualifier'],
        provisional=tiers['provisional']), dec, per_squad


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def dump(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')


def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    mat = Material(data_dir)

    # the three captures are told apart by the identifier in the envelope
    roles = {}
    for name, path in mat.captures.items():
        env, _ = parse_capture(path)
        roles.setdefault(env['capture_id'] % 10, []).append((name, path))
    for role in (1, 2, 3):
        if role not in roles or not roles[role]:
            raise SystemExit('no capture with an identifier ending in %d' % role)
    reference, control, season = (roles[1][0], roles[2][0], roles[3][0])

    audit = {'captures': {'reference': reference[0], 'control': control[0],
                          'season': season[0]},
             'guard_bits': mat.guard_bits,
             'published_rosters': {
                 'engine_tag': mat.engine_tags, 'window_depth': mat.window_depths,
                 'sentinel': mat.sentinel_roster, 'season_budget': mat.budgets,
                 'promotion_threshold': mat.thresholds}}

    # ---- the roster dictionary: literal wire keys of the reference capture
    seed = decode_capture(reference[1], mat.engine_tags[0], mat.window_depths[0],
                          mat.sentinels[0], [], mat.guard_bits)
    dictionary = []
    for rec in seed['records']:
        if rec['mode'] == 0 and rec['wire_key'] not in dictionary:
            dictionary.append(rec['wire_key'])
    audit['roster_dictionary'] = dictionary

    transcript = {'present': mat.transcript is not None, 'compared': False,
                  'records_compared': 0, 'byte_exact_agreement': None}
    if mat.transcript is not None:
        with open(mat.transcript, 'r', encoding='utf-8') as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        rebuilt = [dict(kind=r['kind'], logical_segment=r['logical_segment'],
                        match_id=r['match_id'], record_ordinal=r['record_ordinal'],
                        squad_key=r['wire_key'], tick=r['tick'], unit=r['unit'],
                        value=r['value']) for r in seed['records']]
        transcript.update(compared=True, records_compared=len(rows),
                          byte_exact_agreement=(rebuilt == rows))
    audit['reference_transcript_check'] = transcript

    labels = mat.figure_label
    def published(key):
        return mat.controls[labels[key]][1]
    guard_keys = FIGURE_KEYS[:6]
    budget_keys = FIGURE_KEYS[6:8]
    threshold_keys = FIGURE_KEYS[8:]

    # ---- stage one: the guard quantities, pinned by the first six figures
    grid = []
    survivors = []
    for tag in mat.engine_tags:
        for depth in mat.window_depths:
            for sentinel in mat.sentinels:
                cell = {'engine_tag': tag, 'window_depth': depth,
                        'sentinel': sentinel, 'verdict': 'rejected',
                        'falsifier': None, 'detail': None}
                try:
                    snap, _dec, _ = control_reading(
                        mat, control[1], tag, depth, sentinel, dictionary)
                    for key in guard_keys:
                        if snap[key] != published(key):
                            cell['falsifier'] = labels[key]
                            cell['detail'] = 'published %s, this reading gives %s' % (
                                published(key), snap[key])
                            break
                    else:
                        cell['verdict'] = 'accepted'
                        survivors.append(cell)
                except Exception as exc:
                    cell['falsifier'] = labels['squads']
                    cell['detail'] = 'unreadable under this reading: %s' % exc
                grid.append(cell)
    if not survivors:
        raise SystemExit('no reading of the guard quantities reproduces the controls')
    chosen = survivors[0]
    tag, depth, sentinel = chosen['engine_tag'], chosen['window_depth'], chosen['sentinel']
    sentinel_label = mat.sentinel_roster[mat.sentinels.index(sentinel)]
    for cell in survivors[1:]:
        cell['verdict'] = 'accepted'
        cell['note'] = 'equifinal with the selected reading'

    def per_quantity(field, roster, values=None):
        # the sentinel roster is published in hex; the searched values are ints
        values = list(roster) if values is None else values
        rows = []
        for value, searched in zip(roster, values):
            cells = [c for c in grid if c[field] == searched]
            accepted = [c for c in cells if c['verdict'] == 'accepted']
            if accepted:
                rows.append({'value': value, 'verdict': 'accepted',
                             'falsifier': None, 'detail': None})
            else:
                worst = None
                for cell in cells:
                    if cell['falsifier'] is None:
                        continue
                    rank = mat.control_labels.index(cell['falsifier'])
                    if worst is None or rank < worst[0]:
                        worst = (rank, cell)
                rows.append({'value': value, 'verdict': 'rejected',
                             'falsifier': worst[1]['falsifier'] if worst else None,
                             'detail': worst[1]['detail'] if worst else None})
        return rows

    audit['unstated_quantities'] = {
        'engine_tag': {'roster': mat.engine_tags, 'selected': tag,
                       'candidates': per_quantity('engine_tag', mat.engine_tags)},
        'window_depth': {'roster': mat.window_depths, 'selected': depth,
                         'candidates': per_quantity('window_depth',
                                                    mat.window_depths)},
        'sentinel': {'roster': mat.sentinel_roster,
                     'selected': mat.sentinel_roster[mat.sentinels.index(sentinel)],
                     'candidates': per_quantity('sentinel', mat.sentinel_roster,
                                                mat.sentinels)}}

    # ---- stage two: the attribution reading and the season budget
    # The hand over settlement could be read two ways: a recovered event keeps
    # the squad that recorded it, or it follows that squad's entry to its
    # effective owner.  The budget is carried by squads, so the published
    # charged and overflow upkeep settle the question together with the cap.
    readings = [('recording squad', False), ('effective owner', True)]
    reading_rows = []
    joint = {}
    for reading_name, reattribute in readings:
        _snap, _dec, ctrl_squads = control_reading(
            mat, control[1], tag, depth, sentinel, dictionary,
            reattribute=reattribute)
        survivors = []
        for cap in mat.budgets:
            charged = overflow = 0
            for acc in ctrl_squads.values():
                left = cap
                for _l, raw in sorted(acc['rows']):
                    take = min(raw, left)
                    charged += take
                    overflow += raw - take
                    left -= take
            if all((charged if key == 'charged' else overflow) == published(key)
                   for key in budget_keys):
                survivors.append(cap)
        joint[reading_name] = (survivors, reattribute, ctrl_squads)
        row = {'value': reading_name, 'verdict': 'rejected', 'falsifier': None,
               'detail': None}
        if survivors:
            row['verdict'] = 'accepted'
            row['detail'] = 'reproduces the published upkeep with the cap %s' % survivors[0]
        else:
            killed = labels['overflow'] if published('overflow') == 0 else labels['charged']
            row['falsifier'] = killed
            row['detail'] = ('no cap on the published roster reproduces the charged '
                             'and overflow upkeep under this reading')
        reading_rows.append(row)
    live = [(name, joint[name][1]) for name, _f in readings if joint[name][0]]
    if not live:
        raise SystemExit('neither attribution reading reproduces the control upkeep')
    reading, reattribute = live[0]
    budget = joint[reading][0][0]
    ctrl_squads = joint[reading][2]
    audit['attribution_reading'] = {
        'candidate_space': [r for r, _f in readings], 'selected': reading,
        'candidates': reading_rows,
        'note': ('the control slice carries hand overs of its own, and only this '
                 'reading reproduces the published charged and overflow upkeep')}

    budget_rows = []
    for cap in mat.budgets:
        row = {'value': cap, 'verdict': 'rejected', 'falsifier': None, 'detail': None}
        charged = overflow = 0
        for acc in ctrl_squads.values():
            left = cap
            for _l, raw in sorted(acc['rows']):
                take = min(raw, left)
                charged += take
                overflow += raw - take
                left -= take
        for key in budget_keys:
            got = charged if key == 'charged' else overflow
            if got != published(key):
                row['falsifier'] = labels[key]
                row['detail'] = 'published %s, this cap gives %s' % (
                    published(key), got)
                break
        else:
            row['verdict'] = 'accepted'
        budget_rows.append(row)
    audit['unstated_quantities']['season_budget'] = {
        'roster': mat.budgets, 'selected': budget, 'candidates': budget_rows}

    # ---- stage three: the promotion threshold
    threshold_rows = []
    threshold_survivors = []
    for value in mat.thresholds:
        row = {'value': value, 'verdict': 'rejected', 'falsifier': None, 'detail': None}
        snap, _d, _p = control_reading(mat, control[1], tag, depth, sentinel,
                                       dictionary, budget, value,
                                       reattribute=reattribute)
        for key in threshold_keys:
            if snap[key] != published(key):
                row['falsifier'] = labels[key]
                row['detail'] = 'published %s, this threshold gives %s' % (
                    published(key), snap[key])
                break
        else:
            row['verdict'] = 'accepted'
            threshold_survivors.append(value)
        threshold_rows.append(row)
    if not threshold_survivors:
        raise SystemExit('no promotion threshold reproduces the controls')
    threshold = threshold_survivors[0]
    audit['unstated_quantities']['promotion_threshold'] = {
        'roster': mat.thresholds, 'selected': threshold,
        'candidates': threshold_rows}

    reproduced, control_dec, _ = control_reading(mat, control[1], tag, depth,
                                                 sentinel, dictionary, budget,
                                                 threshold, reattribute=reattribute)
    audit['calibration'] = {
        'control_capture': control[0],
        'reading': {'engine_tag': tag, 'window_depth': depth,
                    'sentinel': sentinel_label,
                    'season_budget': budget, 'promotion_threshold': threshold,
                    'attribution': reading},
        'rows': [{'figure': '%s %s' % (lbl, mat.controls[lbl][0]),
                  'published': mat.controls[lbl][1],
                  'reproduced': reproduced[key], 'agree':
                      reproduced[key] == mat.controls[lbl][1]}
                 for lbl, key in zip(mat.control_labels, FIGURE_KEYS)]}

    # ---- the season
    season_dec = decode_capture(season[1], tag, depth, sentinel, dictionary,
                                mat.guard_bits)
    records = season_dec['records']

    handover, owner, passes, squads_seen = ownership_of(records, dictionary)
    redirected = sum(1 for r in records if owner[r['owner']] != r['owner'])
    audit['attribution_settlement'] = {
        'reading': reading,
        'handovers': [{'from': k, 'to': v} for k, v in sorted(handover.items())],
        'effective_owners': {k: owner[k] for k in sorted(owner)},
        'pass_count': passes,
        'records_following_a_hand_over': redirected,
        'squads_that_handed_their_entry_away': sorted(handover),
        'squads_no_longer_their_own_effective_owner':
            sorted(k for k in owner if owner[k] != k),
        'decode_notes': season_dec['notes']}

    def settles(key):
        """The squad a recovered event settles on, under the selected reading."""
        return owner[key] if reattribute else key

    # ---- per match
    by_segment = {}
    for rec in records:
        by_segment.setdefault(rec['logical_segment'], []).append(rec)
    matches = []
    for logical in sorted(by_segment):
        recs = by_segment[logical]
        seg = [s for s in season_dec['segments'] if s['logical_position'] == logical][0]
        close = max([r['tick'] for r in recs if r['kind'] == 8], default=None)
        figs, conceding = match_figures(recs, close)
        rows = {}
        for key, fig in figs.items():
            target = settles(key)
            row = rows.setdefault(target, dict(squad_key=target, gross_output=0,
                                               voided_output=0, raw_upkeep=0,
                                               conceded=False, members=[]))
            row['gross_output'] += fig['gross_output']
            row['voided_output'] += fig['voided_output']
            row['raw_upkeep'] += fig['raw_upkeep']
            row['members'].append(key)
            if key in conceding:
                row['conceded'] = True
        escapes = sum(1 for r in recs if r['directives'])
        review_index = sum(v['voided_output'] for v in rows.values()) \
            + mat.review_weight * escapes
        ranked = [(v['gross_output'], k) for k, v in rows.items() if mat.registry.get(k)]
        if ranked:
            best = max(g for g, _k in ranked)
            conference = mat.meta(sorted(k for g, k in ranked if g == best)[0])['conference']
        else:
            conference = None
        matches.append(dict(logical_segment=logical, match_id=seg['match_id'],
                            close_tick=close, rows=rows, review_index=review_index,
                            conference=conference, records=len(recs),
                            escape_records=escapes))

    # ---- per squad
    per = {}
    for m in matches:
        for key in m['rows']:
            if key not in per:
                per[key] = _new_squad(mat, key, owner, handover)
    for key in squads_seen:
        per.setdefault(key, _new_squad(mat, key, owner, handover))

    for m in matches:
        for key, row in m['rows'].items():
            p = per[key]
            p['gross_output'] += row['gross_output']
            p['voided_output'] += row['voided_output']
            p['raw_upkeep'] += row['raw_upkeep']
            p['matches'] += 1
            p['match_rows'].append(dict(logical_segment=m['logical_segment'],
                                        match_id=m['match_id'],
                                        gross_output=row['gross_output'],
                                        voided_output=row['voided_output'],
                                        raw_upkeep=row['raw_upkeep'],
                                        conceded=row['conceded']))

    for p in per.values():
        left = budget
        for row in sorted(p['match_rows'], key=lambda x: x['logical_segment']):
            take = min(row['raw_upkeep'], left)
            row['charged_upkeep'] = take
            row['overflow_upkeep'] = row['raw_upkeep'] - take
            left -= take
        p['charged_upkeep'] = sum(r['charged_upkeep'] for r in p['match_rows'])
        p['overflow_upkeep'] = sum(r['overflow_upkeep'] for r in p['match_rows'])
        p['season_score'] = (p['gross_output'] * p['ladder_weight']
                             - p['charged_upkeep'] * p['levy_rate'])
        p['tier'] = tier_of(p['season_score'], threshold)

    # ---- standings
    for m in matches:
        scores = {}
        for key, row in m['rows'].items():
            charged = [r for r in per[key]['match_rows']
                       if r['logical_segment'] == m['logical_segment']][0]['charged_upkeep']
            md = mat.meta(key)
            scores[key] = (row['gross_output'] * md['ladder_weight']
                           - charged * md['levy_rate'])
        standing = [k for k in scores if not m['rows'][k]['conceded']]
        winners = []
        if standing:
            top = max(scores[k] for k in standing)
            winners = sorted(k for k in standing if scores[k] == top)
        m['match_scores'] = scores
        m['winners'] = winners
        m['outright'] = len(winners) == 1
        for key in scores:
            if key in winners:
                per[key]['standing_points'] += 3 if m['outright'] else 1
            per[key]['results'].append('win' if (m['outright'] and key in winners)
                                       else 'shared' if key in winners else 'other')
    for p in per.values():
        run = streak = 0
        for res in p['results']:
            if res == 'win':
                run += 1
                if run >= 2:
                    streak += 2
            else:
                run = 0
        p['streak_credit'] = streak
        p['standing_points'] += streak

    ladder = sorted(per.values(), key=lambda p: (-p['season_score'],
                                                 -p['standing_points'],
                                                 p['squad_key']))
    for i, p in enumerate(ladder, 1):
        p['rank'] = i

    # ---- finals bracket
    pool = [p for p in per.values()
            if p['effective_owner'] == p['squad_key']
            and p['matches'] >= mat.eligibility_floor]
    constraint_names = ['ladder weight prominence', 'both entrant segments',
                        'standing points floor', 'no shared conference',
                        'a ranked entrant', 'combined ladder weight floor',
                        'no overflow upkeep', 'a champion tier',
                        'combined gross output floor', 'no entry handed away']

    def judge(members):
        conferences = [m['conference'] for m in members if m['conference']]
        segments = set(m['segment'] for m in members)
        return [
            any(m['ladder_weight'] >= mat.prominence for m in members),
            all(seg in segments for seg in mat.segments),
            all(m['standing_points'] >= mat.standing_floor for m in members),
            len(conferences) == len(set(conferences)),
            any(m['is_ranked'] for m in members),
            sum(m['ladder_weight'] for m in members) >= mat.weight_floor,
            all(m['overflow_upkeep'] == 0 for m in members),
            any(m['tier'] == 'champion' for m in members),
            sum(m['gross_output'] for m in members) >= mat.gross_floor,
            all(m['handed_away'] == 0 for m in members),
        ]

    best = None
    space = 0
    for combo in combinations(sorted(pool, key=lambda p: p['squad_key']), 3):
        space += 1
        met = judge(combo)
        score = sum(met)
        charged = sum(m['charged_upkeep'] for m in combo)
        keys = tuple(m['squad_key'] for m in combo)
        cand = {'score': score, 'charged': charged, 'keys': keys,
                'members': combo, 'met': met}
        if best is None or score > best['score'] or (
                score == best['score'] and (charged < best['charged'] or
                                            (charged == best['charged'] and
                                             keys < best['keys']))):
            best = cand
    if best is None:
        raise SystemExit('no eligible bracket candidates')
    bracket = {
        'members': [_bracket_member(m) for m in best['members']],
        'satisfied_constraints': best['score'],
        'constraint_count': len(constraint_names),
        'constraint_detail': dict(zip(constraint_names, best['met'])),
        'total_charged_upkeep': best['charged'],
    }
    audit['bracket_search'] = {
        'eligibility': {'is own effective owner': True,
                        'match floor': mat.eligibility_floor},
        'eligible_squads': sorted(p['squad_key'] for p in pool),
        'candidate_space': space,
        'best_satisfied_constraints': best['score'],
        'tie_breaks': ['least total charged upkeep', 'member keys sort first']}

    # ---- review docket
    docket_pool = [m for m in matches if m['conference'] is not None]
    best_docket = None
    for combo in combinations(sorted(docket_pool, key=lambda m: m['match_id']),
                              mat.docket_size):
        counts = Counter(m['conference'] for m in combo)
        if any(v > mat.docket_cap for v in counts.values()):
            continue
        total = sum(m['review_index'] for m in combo)
        ids = tuple(m['match_id'] for m in combo)
        cand = {'total': total, 'ids': ids, 'combo': combo}
        if best_docket is None or total > best_docket['total'] or (
                total == best_docket['total'] and ids < best_docket['ids']):
            best_docket = cand
    if best_docket is None:
        raise SystemExit('no docket satisfies the conference cap')
    docket = {
        'size': len(best_docket['combo']),
        'matches': [dict(match_id=m['match_id'], conference=m['conference'],
                         review_index=m['review_index'],
                         voided_output=sum(v['voided_output'] for v in m['rows'].values()),
                         escape_directive_records=m['escape_records'],
                         records=m['records']) for m in best_docket['combo']],
        'total_review_index': best_docket['total'],
        'conference_counts': dict(Counter(m['conference'] for m in best_docket['combo'])),
    }
    audit['docket_search'] = {
        'docket size': mat.docket_size, 'conference cap': mat.docket_cap,
        'review weight': mat.review_weight,
        'matches_available': len(matches),
        'matches_with_a_conference': len(docket_pool),
        'conference_counts_across_the_season':
            dict(Counter(m['conference'] for m in matches))}

    # ---- reports
    def entry(p):
        return dict(
            squad_key=p['squad_key'], entrant_id=p['entrant_id'],
            division=p['division'], conference=p['conference'],
            licence_class=p['licence_class'], segment=p['segment'],
            is_ranked=p['is_ranked'], matches=p['matches'],
            gross_output=p['gross_output'], voided_output=p['voided_output'],
            charged_upkeep=p['charged_upkeep'],
            overflow_upkeep=p['overflow_upkeep'],
            season_score=p['season_score'], standing_points=p['standing_points'],
            streak_credit=p['streak_credit'], tier=p['tier'], rank=p['rank'],
            is_own_effective_owner=p['effective_owner'] == p['squad_key'])

    def group_report(kind, name, members):
        regs = sorted((p for p in members if p['registered']),
                      key=lambda p: (p['entrant_id'] or '', p['squad_key']))
        tiers = Counter(p['tier'] for p in regs)
        rep = dict(
            group_kind=kind, group_name=name, entrants=len(regs),
            entries=[entry(p) for p in regs],
            gross_output=sum(p['gross_output'] for p in regs),
            voided_output=sum(p['voided_output'] for p in regs),
            charged_upkeep=sum(p['charged_upkeep'] for p in regs),
            overflow_upkeep=sum(p['overflow_upkeep'] for p in regs),
            season_score=sum(p['season_score'] for p in regs),
            standing_points=sum(p['standing_points'] for p in regs),
            streak_credit=sum(p['streak_credit'] for p in regs),
            tier_counts={t: tiers.get(t, 0) for t in TIER_NAMES})
        if kind == 'conference':
            rep['divisions'] = sorted(set(p['division'] for p in regs if p['division']))
        else:
            rep['licence_classes'] = sorted(set(p['licence_class'] for p in regs
                                                if p['licence_class']))
        return rep

    reports = {}
    for conf in mat.conferences:
        reports['region_%s.json' % conf] = group_report(
            'conference', conf, [p for p in per.values() if p['conference'] == conf])
    for seg in mat.segments:
        reports['segment_%s.json' % seg] = group_report(
            'segment', seg, [p for p in per.values() if p['segment'] == seg])

    quality = {'captures': {}}
    for role, (name, path), dec in (('reference', reference, seed),
                                    ('control', control, control_dec),
                                    ('season', season, season_dec)):
        env = dec['env']
        seen = []
        for r in dec['records']:
            key = r['wire_key'] if r['mode'] == 0 else r['owner']
            if key not in seen:
                seen.append(key)
        wire_only = sorted(set(
            r['wire_key'] for r in dec['records'] if isinstance(r['wire_key'], str)))
        unregistered = sorted(k for k in seen if not mat.registry.get(k))
        # a squad can be recovered without ever being written down as a literal key
        never_written = sorted(k for k in seen if k not in wire_only)
        bias = dec['count_bias']
        ledger = Counter(KIND_NAMES.get(r['kind'], 'kind%d' % r['kind'])
                         for r in dec['records'])
        quality['captures'][name] = dict(
            role=role, capture_id=env['capture_id'],
            guard_register=env['guard_register'], active_guards=env['guards'],
            count_bias_applied=bias,
            envelope_checksum_agrees=env['envelope_checksum_agrees'],
            payload_checksums_agree=all(s['payload_checksum_agrees'] for s in dec['segments']),
            declared_segment_count=env['declared_segment_count'] - bias,
            segments_recovered=len(dec['segments']),
            segment_residual=len(dec['segments']) - (env['declared_segment_count'] - bias),
            declared_record_count=env['declared_record_count'] - bias,
            records_recovered=len(dec['records']),
            record_residual=len(dec['records']) - (env['declared_record_count'] - bias),
            declared_roster_size=env['declared_roster_size'] - bias,
            distinct_squads_recovered=len(seen),
            registered_squads_recovered=len(seen) - len(unregistered),
            roster_residual=len(seen) - (env['declared_roster_size'] - bias),
            registered_keys_written_on_the_wire=len(
                [k for k in wire_only if mat.registry.get(k)]),
            unregistered_squads=unregistered,
            squads_seen_on_the_wire=wire_only,
            squads_recovered_but_never_written_on_the_wire=never_written,
            filler_width_matches_written_salt=all(
                s['filler_width_matches_written_salt'] for s in dec['segments']),
            filler_width_matches_effective_salt=all(
                s['filler_width_matches_effective_salt'] for s in dec['segments']),
            tick_backsteps=sum(s['tick_backsteps'] for s in dec['segments']),
            kind_ledger={k: ledger.get(k, 0) for k in LEDGER_ORDER},
            matches_in_logical_order=[s['match_id'] for s in
                                      sorted(dec['segments'],
                                             key=lambda x: x['logical_position'])],
            decode_notes=dec['notes'])

    season_gross = sum(p['gross_output'] for p in per.values())
    season_voided = sum(p['voided_output'] for p in per.values())
    conference_gross = sum(p['gross_output'] for p in per.values() if p['conference'])
    raw_total = sum(p['raw_upkeep'] for p in per.values())
    charged_total = sum(p['charged_upkeep'] for p in per.values())
    overflow_total = sum(p['overflow_upkeep'] for p in per.values())
    season_ledger = Counter(KIND_NAMES.get(r['kind'], 'kind%d' % r['kind'])
                            for r in records)
    quality['season'] = dict(
        squads_settled=len(per),
        registered_squads=sum(1 for p in per.values() if p['registered']),
        unregistered_squads=sorted(p['squad_key'] for p in per.values()
                                   if not p['registered']),
        absent_entrants=sorted(v['entrant_id'] for k, v in mat.registry.items()
                               if k not in per),
        season_gross_output=season_gross,
        conference_gross_output=conference_gross,
        attribution_residual=season_gross - conference_gross,
        output_reported_in_the_capture=sum(
            r['value'] for r in records if r['kind'] == 2),
        gross_plus_voided=season_gross + season_voided,
        output_ledger_residual=(season_gross + season_voided)
        - sum(r['value'] for r in records if r['kind'] == 2),
        raw_upkeep=raw_total,
        charged_plus_overflow=charged_total + overflow_total,
        upkeep_ledger_residual=(charged_total + overflow_total) - raw_total,
        kind_ledger={k: season_ledger.get(k, 0) for k in LEDGER_ORDER},
        tick_backsteps=sum(s['tick_backsteps'] for s in season_dec['segments']))

    tiers = Counter(p['tier'] for p in per.values())
    summary = dict(
        captures={'reference': reference[0], 'control': control[0],
                  'season': season[0]},
        reading={'engine_tag': tag, 'window_depth': depth,
                 'sentinel': sentinel_label,
                 'season_budget': budget, 'promotion_threshold': threshold},
        matches=len(matches), records_recovered=len(records),
        squads_settled=len(per),
        registered_squads=sum(1 for p in per.values() if p['registered']),
        unregistered_squads=quality['season']['unregistered_squads'],
        absent_entrants=quality['season']['absent_entrants'],
        attribution_settlement_passes=passes,
        handovers=audit['attribution_settlement']['handovers'],
        gross_output=season_gross, voided_output=season_voided,
        raw_upkeep=raw_total, charged_upkeep=charged_total,
        overflow_upkeep=overflow_total,
        season_score=sum(p['season_score'] for p in per.values()),
        standing_points=sum(p['standing_points'] for p in per.values()),
        streak_credit=sum(p['streak_credit'] for p in per.values()),
        tier_counts={t: tiers.get(t, 0) for t in TIER_NAMES},
        ladder=[dict(rank=p['rank'], squad_key=p['squad_key'],
                     entrant_id=p['entrant_id'], conference=p['conference'],
                     division=p['division'], segment=p['segment'],
                     matches=p['matches'], season_score=p['season_score'],
                     tier=p['tier'], standing_points=p['standing_points'],
                     streak_credit=p['streak_credit'],
                     gross_output=p['gross_output'],
                     voided_output=p['voided_output'],
                     charged_upkeep=p['charged_upkeep'],
                     overflow_upkeep=p['overflow_upkeep']) for p in ladder],
        finals_bracket=bracket,
        review_docket=docket,
        conferences={c: dict(
            entrants=sum(1 for p in per.values() if p['conference'] == c),
            gross_output=sum(p['gross_output'] for p in per.values()
                             if p['conference'] == c),
            season_score=sum(p['season_score'] for p in per.values()
                             if p['conference'] == c)) for c in mat.conferences},
        segments={s: dict(
            entrants=sum(1 for p in per.values() if p['segment'] == s),
            gross_output=sum(p['gross_output'] for p in per.values()
                             if p['segment'] == s),
            season_score=sum(p['season_score'] for p in per.values()
                             if p['segment'] == s)) for s in mat.segments})

    reports['summary.json'] = summary
    reports['data_quality.json'] = quality
    reports['policy_audit.json'] = audit

    for name, obj in reports.items():
        dump(os.path.join(out_dir, name), obj)
    return 0


def _new_squad(mat, key, owner, handover):
    md = mat.meta(key)
    return dict(squad_key=key, registered=md['registered'],
                entrant_id=md['entrant_id'], division=md['division'],
                conference=md['conference'], ladder_weight=md['ladder_weight'],
                licence_class=md['licence_class'], segment=md['segment'],
                levy_rate=md['levy_rate'], is_ranked=md['is_ranked'],
                effective_owner=owner.get(key, key),
                handed_away=1 if key in handover else 0,
                gross_output=0, voided_output=0, raw_upkeep=0, charged_upkeep=0,
                overflow_upkeep=0, matches=0, standing_points=0, streak_credit=0,
                season_score=0, tier='provisional', rank=None,
                match_rows=[], results=[])


def _bracket_member(p):
    return dict(squad_key=p['squad_key'], entrant_id=p['entrant_id'],
                conference=p['conference'], division=p['division'],
                segment=p['segment'], licence_class=p['licence_class'],
                is_ranked=p['is_ranked'], ladder_weight=p['ladder_weight'],
                matches=p['matches'], gross_output=p['gross_output'],
                voided_output=p['voided_output'],
                charged_upkeep=p['charged_upkeep'],
                overflow_upkeep=p['overflow_upkeep'],
                standing_points=p['standing_points'],
                streak_credit=p['streak_credit'],
                season_score=p['season_score'], tier=p['tier'], rank=p['rank'])


if __name__ == '__main__':
    sys.exit(main(sys.argv))
