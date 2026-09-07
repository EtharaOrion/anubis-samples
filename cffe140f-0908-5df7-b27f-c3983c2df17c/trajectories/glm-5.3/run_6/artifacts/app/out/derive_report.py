#!/usr/bin/env python3
"""Settle a tactics league season from sealed replay journals.

Usage:
    python3 derive_report.py DATA_DIR OUT_DIR

DATA_DIR holds the published league material (markdown contracts, csv tables and
the sealed .rjnl captures); OUT_DIR receives the nine settlement reports.  Both
directories are taken from the arguments and nothing else is read or written.

The program implements the written contract in general: every guard is honoured
only when its bit is set in the capture's guard register, the guard quantities
that the material never states (engine tag, window depth, sentinel) and the two
unstated policy figures (season budget, promotion threshold) are recovered by
searching their published rosters against the control figures, and every report
is derived from the sealed season capture alone.
"""

import csv
import hashlib
import json
import os
import re
import struct
import sys
from collections import defaultdict
from itertools import combinations
from math import gcd

MASK32 = 0xFFFFFFFF

# Guard register bit order, as published in replay-guards.md.
GUARD_BITS = [
    'keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
    'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape',
]

# Event kinds, as published in ladder-brief.md.
KIND_LEDGER = [
    (1, 'deploy'), (2, 'output'), (3, 'eliminate'), (4, 'disband'),
    (5, 'concede'), (6, 'transfer'), (8, 'close'),
]
KIND_NAMES = dict(KIND_LEDGER)
KNOWN_KINDS = set(KIND_NAMES)

# Roles, told apart by the capture identifier carried in the envelope.
ROLE_BY_CAPID_SUFFIX = {1: 'reference', 2: 'control', 3: 'season'}


class DecodeError(Exception):
    """A candidate reading of the guards does not hold for a capture."""


# --------------------------------------------------------------------------
# Published material
# --------------------------------------------------------------------------

def read_text(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def parse_roster(text, name):
    """Return the backticked items of the published roster line `Name roster:`."""
    for line in text.splitlines():
        m = re.match(r'^\s*(.+?)\s+roster\s*:\s*(.+?)\s*$', line)
        if m and m.group(1).strip().lower() == name.lower():
            return [v.strip() for v in re.findall(r'`([^`]*)`', m.group(2)) if v.strip()]
    raise ValueError('published roster %r not found' % name)


def parse_constants(text):
    """Return every `Label: `value`` line of a policy document as ints."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*`(-?\d+)`\s*$', line)
        if m:
            out[m.group(1).strip().lower()] = int(m.group(2))
    return out


def parse_calibration(text):
    """Return the published control figures as an ordered list of (id,label,value)."""
    figs = []
    for line in text.splitlines():
        m = re.match(r'^\s*-\s*(\S+)\s+(.+?)\s*:\s*(-?\d+)\s*$', line)
        if m:
            figs.append((m.group(1), m.group(2).strip().lower(), int(m.group(3))))
    if not figs:
        raise ValueError('no published control figures found')
    return figs


def parse_tier_names(standings_text):
    """Return the promotion tier names in the order they are published."""
    names = []
    for line in standings_text.splitlines():
        m = re.match(r'^\s*-?\s*(?:a|a season|season)?\s*score of.*?\bis\s+(\w+)\s*\.\s*$', line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    if not names:
        names = ['champion', 'contender', 'qualifier', 'provisional']
    return names


def load_static(data_dir):
    guards_md = read_text(os.path.join(data_dir, 'replay-guards.md'))
    standings_md = read_text(os.path.join(data_dir, 'standings-policy.md'))
    upkeep_md = read_text(os.path.join(data_dir, 'upkeep-policy.md'))
    bracket_md = read_text(os.path.join(data_dir, 'bracket-policy.md'))
    calib_md = read_text(os.path.join(data_dir, 'calibration-controls.md'))

    static = {
        'engine_tags': parse_roster(guards_md, 'Engine tag'),
        'window_depths': [int(v) for v in parse_roster(guards_md, 'Window depth')],
        'sentinels': [int(v, 0) for v in parse_roster(guards_md, 'Sentinel')],
        'thresholds': [int(v) for v in parse_roster(standings_md, 'Promotion threshold')],
        'budgets': [int(v) for v in parse_roster(upkeep_md, 'Budget cap')],
        'tier_names': parse_tier_names(standings_md),
        'calibration': parse_calibration(calib_md),
        'bracket': parse_constants(bracket_md),
    }
    for key, label in [
        ('eligibility_floor', 'eligibility match floor'),
        ('prominence', 'ladder weight prominence'),
        ('standing_floor', 'standing points floor'),
        ('weight_floor', 'combined ladder weight floor'),
        ('gross_floor', 'combined gross output floor'),
        ('review_weight', 'review weight'),
        ('docket_size', 'docket size'),
        ('docket_cap', 'docket conference cap'),
    ]:
        if label not in static['bracket']:
            raise ValueError('published figure %r not found' % label)
        static[key] = static['bracket'][label]

    divisions, conferences = {}, []
    with open(os.path.join(data_dir, 'division-topology.csv'), newline='') as fh:
        for row in csv.DictReader(fh):
            divisions[row['division']] = (row['conference'], int(row['ladder_weight']))
            if row['conference'] not in conferences:
                conferences.append(row['conference'])
    licences, segments = {}, []
    with open(os.path.join(data_dir, 'licence-classes.csv'), newline='') as fh:
        for row in csv.DictReader(fh):
            licences[row['licence_class']] = (row['segment'], int(row['levy_rate']))
            if row['segment'] not in segments:
                segments.append(row['segment'])
    registry = {}
    with open(os.path.join(data_dir, 'entrant-registry.csv'), newline='') as fh:
        for row in csv.DictReader(fh):
            registry[row['squad_key']] = {
                'entrant_id': row['entrant_id'],
                'division': row['division'],
                'licence_class': row['licence_class'],
                'is_ranked': row['is_ranked'].strip().lower() in ('yes', 'true', 'y', '1'),
            }
    static.update(divisions=divisions, conferences=conferences,
                  licences=licences, segments=segments, registry=registry)
    return static


# --------------------------------------------------------------------------
# Captures
# --------------------------------------------------------------------------

class Capture(object):
    """A sealed journal: the thirty six byte envelope plus its segments."""

    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        with open(path, 'rb') as fh:
            data = fh.read()
        if len(data) < 36 or data[0:4] != b'RJNL':
            raise ValueError('%s is not an RJNL capture' % self.filename)
        self.guard_register = struct.unpack('<H', data[6:8])[0]
        self.nonce_le = struct.unpack('<I', data[8:12])[0]
        self.nonce_be = struct.unpack('>I', data[8:12])[0]
        (self.declared_segments, self.declared_records, self.base_tick,
         self.capture_id, self.declared_roster, self.checksum) = (
            struct.unpack('<6I', data[12:36]))
        self.envelope_checksum_ok = (sum(data[0:32]) & MASK32) == self.checksum
        self.segments = []
        off = 36
        while off < len(data):
            if data[off:off + 4] != b'SEG0' or off + 28 > len(data):
                raise ValueError('%s: bad segment envelope at %d' % (self.filename, off))
            slot, mid, count, salt, width, chk = struct.unpack('<6I', data[off + 4:off + 28])
            off += 28
            payload = data[off:off + width]
            if len(payload) != width:
                raise ValueError('%s: short segment payload' % self.filename)
            off += width
            self.segments.append({
                'slot': slot, 'match_id': mid, 'declared_count': count, 'salt': salt,
                'width': width, 'checksum': chk,
                'checksum_ok': (sum(payload) & MASK32) == chk,
                'payload': payload,
            })

    def on(self, guard):
        return bool((self.guard_register >> GUARD_BITS.index(guard)) & 1)

    @property
    def active_guards(self):
        return [g for g in GUARD_BITS if self.on(g)]

    @property
    def nonce(self):
        return self.nonce_be if self.on('endian') else self.nonce_le


def permutation_step(nonce, count):
    """The segperm step: least integer >= ((nonce>>8) mod N)+2 coprime with N."""
    floor = ((nonce >> 8) % count) + 2
    step = floor
    while gcd(step, count) != 1:
        step += 1
    return step


def rotate_right(blob, turn):
    if not blob:
        return blob
    turn %= len(blob)
    return blob[-turn:] + blob[:-turn] if turn else blob


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def decode_capture(cap, engine_tag, sentinel, window, dictionary=None):
    """Decode one capture under a candidate reading of the guards.

    Returns a dict with the recovered records and the per capture figures the
    reconciliation needs.  Raises DecodeError when the reading does not hold.
    """
    bias = (sum(engine_tag.encode('latin1')) % 11) if cap.on('countbias') else 0
    nonce = cap.nonce
    total = len(cap.segments)

    if cap.on('segperm'):
        step = permutation_step(nonce, total)
        logical = [None] * total
        for seg in cap.segments:
            position = (nonce + seg['slot'] * step) % total
            if logical[position] is not None:
                raise DecodeError('segperm collision')
            logical[position] = seg
        if any(s is None for s in logical):
            raise DecodeError('segperm does not cover the capture')
    else:
        logical = sorted(cap.segments, key=lambda s: s['slot'])
        step = None

    records = []
    backwards_ticks = 0
    previous_tick = nonce          # the chain runs across segments
    capture_ordinal = 0
    recent = []                    # rolling list of recently seen squads
    drift = 0                      # running sum of value fields of earlier segments

    for index, seg in enumerate(logical):
        salt = (seg['salt'] ^ drift) & MASK32 if (cap.on('driftkey') and index) else seg['salt']
        count = seg['declared_count'] - bias
        if count < 0:
            raise DecodeError('declared record count below the count bias')
        payload = seg['payload']
        pos = 0
        first_difference = 0
        tick = 0

        for ordinal in range(count):
            if pos + 2 > len(payload):
                raise DecodeError('record frame runs past the payload')
            shadow = payload[pos + 1]
            if cap.on('shadow'):
                width = shadow ^ (salt & 0xFF)
            else:
                width = payload[pos]
            if width <= 0 or pos + 2 + width > len(payload):
                raise DecodeError('record blob width out of range')
            blob = payload[pos + 2:pos + 2 + width]
            pos += 2 + width

            if cap.on('keystream'):
                keymat = hashlib.sha256(('%s:%d:%d:%d' % (
                    engine_tag, seg['match_id'], salt, ordinal)).encode('utf-8')).digest()
                blob = bytes(a ^ b for a, b in zip(blob, keymat))
            if cap.on('rotate'):
                blob = rotate_right(blob, salt + capture_ordinal)

            directives = []
            if cap.on('escape'):
                while len(directives) < 2 and blob and blob[0] == sentinel:
                    if len(blob) < 2:
                        raise DecodeError('truncated escape directive')
                    directives.append((blob[1] >> 4, blob[1] & 0x0F))
                    blob = blob[2:]

            if len(blob) < 13:
                raise DecodeError('record body too narrow')
            kind, mode = blob[0], blob[1]
            if kind not in KNOWN_KINDS:
                raise DecodeError('unknown event kind %d' % kind)
            if mode == 0:
                if len(blob) < 15:
                    raise DecodeError('record body too narrow for a named attribution')
                field = blob[2:5].decode('ascii', 'replace')
                if not all('A' <= c <= 'Z' for c in field):
                    raise DecodeError('attribution key is not a squad name')
                body = 5
            elif mode in (1, 2):
                field = blob[2]
                body = 3
            else:
                raise DecodeError('unknown attribution mode %d' % mode)

            unit, = struct.unpack('<H', blob[body:body + 2])
            stored_tick, = struct.unpack('<I', blob[body + 2:body + 6])
            stored_value, = struct.unpack('<I', blob[body + 6:body + 10])

            if cap.on('delta2'):
                first_difference = (first_difference + stored_tick) & MASK32
                new_tick = (tick + first_difference) & MASK32
            else:
                new_tick = stored_tick
            if new_tick < tick:
                backwards_ticks += 1
            tick = new_tick
            value = (stored_value ^ previous_tick) & MASK32 if cap.on('chain') else stored_value
            previous_tick = tick

            if mode == 0:
                key = field
            elif mode == 1:
                if not cap.on('backref'):
                    raise DecodeError('back reference attribution with the guard idle')
                if field >= len(recent):
                    raise DecodeError('back reference outside the window')
                key = recent[field]
            else:
                if not cap.on('alias'):
                    raise DecodeError('dictionary attribution with the guard idle')
                if dictionary is None:
                    key = None
                else:
                    key = dictionary[field % len(dictionary)]

            # The window is refreshed with the key as written on the wire,
            # before any escape directive has been weighed.
            wire_key = key
            if cap.on('backref') and key is not None:
                if key in recent:
                    recent.remove(key)
                recent.append(key)
                if len(recent) > window:
                    recent.pop(0)

            for op, arg in directives:
                if dictionary is None:
                    key = None
                    continue
                if op == 1:
                    key = dictionary[arg % len(dictionary)]
                elif op == 2:
                    if key in dictionary:
                        key = dictionary[(dictionary.index(key) + arg) % len(dictionary)]
                else:
                    raise DecodeError('unknown escape operation %d' % op)

            records.append({
                'segment': index, 'slot': seg['slot'], 'match_id': seg['match_id'],
                'ordinal': ordinal, 'capture_ordinal': capture_ordinal,
                'kind': kind, 'mode': mode, 'unit': unit, 'tick': tick,
                'value': value, 'directives': directives, 'key': key,
                'wire_key': wire_key, 'effective_salt': salt,
            })
            capture_ordinal += 1
            drift = (drift + value) & MASK32

        filler = len(payload) - pos
        if filler != 3 + (seg['salt'] % 5):
            raise DecodeError('trailing filler width %d does not match the segment salt' % filler)

    return {
        'records': records,
        'logical': [s['match_id'] for s in logical],
        'slots': [s['slot'] for s in logical],
        'bias': bias,
        'step': step,
        'nonce': nonce,
        'backwards_ticks': backwards_ticks,
        'declared_segments': cap.declared_segments - bias,
        'declared_records': cap.declared_records - bias,
        'declared_roster': cap.declared_roster - bias,
    }


def build_dictionary(decoding):
    """The roster dictionary: distinct attribution keys first met in logical order."""
    dictionary = []
    seen = set()
    for rec in decoding['records']:
        if rec['mode'] == 0 and rec['wire_key'] not in seen:
            seen.add(rec['wire_key'])
            dictionary.append(rec['wire_key'])
    return dictionary


def reconcile_transcript(data_dir, decoding):
    """Check a decode against a labelled event transcript shipped beside it."""
    transcripts = sorted(n for n in os.listdir(data_dir) if n.endswith('.jsonl'))
    if not transcripts:
        return None
    path = os.path.join(data_dir, transcripts[0])
    rows = []
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    by_ordinal = {r['record_ordinal']: r for r in rows}
    fields = (('kind', 'kind'), ('match_id', 'match_id'), ('squad_key', 'key'),
              ('tick', 'tick'), ('unit', 'unit'), ('value', 'value'))
    matched = mismatched = 0
    examples = []
    for rec in decoding['records']:
        row = by_ordinal.get(rec['capture_ordinal'])
        if row is None:
            continue
        wrong = [t for t, r in fields if row.get(t) != rec[r]]
        if not wrong and row.get('logical_segment') == rec['segment']:
            matched += 1
        else:
            mismatched += 1
            if len(examples) < 5:
                examples.append({
                    'record_ordinal': rec['capture_ordinal'],
                    'fields': wrong or ['logical_segment'],
                    'transcript': {t: row.get(t) for t, _ in fields},
                    'recovered': {t: rec[r] for t, r in fields},
                })
    return {
        'file': transcripts[0],
        'transcript_records': len(rows),
        'recovered_records': len(decoding['records']),
        'records_compared': matched + mismatched,
        'records_matching': matched,
        'records_differing': mismatched,
        'byte_exact': mismatched == 0 and len(rows) == len(decoding['records']),
        'differences': examples,
    }


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------

def resolve_owners(records, dictionary):
    """Follow the entry hand overs to their end by repeated sweeps."""
    handovers = {}
    for rec in sorted(records, key=lambda r: (r['segment'], r['ordinal'])):
        if rec['kind'] == 6:
            handovers[rec['key']] = dictionary[rec['value'] % len(dictionary)]

    # A closed set of hand overs settles on the member whose key sorts first.
    cycles = []
    state = dict(handovers)
    for cycle in find_cycles(state):
        anchor = min(cycle)
        cycles.append(sorted(cycle))
        for member in cycle:
            state[member] = anchor

    squads = set(handovers) | {r['key'] for r in records}
    owner = {s: s for s in squads}
    sweeps = 0
    while True:
        sweeps += 1
        nxt = {s: state.get(o, o) for s, o in owner.items()}
        if nxt == owner:
            return owner, sweeps, handovers, cycles
        owner = nxt


def find_cycles(handovers):
    cycles, visited = [], set()
    for start in handovers:
        if start in visited:
            continue
        path, positions, cur = [], set(), start
        while cur in handovers and cur not in visited:
            if cur in positions:
                break
            positions.add(cur)
            path.append(cur)
            cur = handovers[cur]
        if cur in positions:
            cycles.append(path[path.index(cur):])
        visited.update(path)
    return cycles


def settle_matches(records):
    """Group the recovered records into matches and score each squad in them."""
    by_segment = defaultdict(list)
    for rec in records:
        by_segment[rec['segment']].append(rec)

    matches = []
    for index in sorted(by_segment):
        recs = by_segment[index]
        per = defaultdict(lambda: {
            'deploys': [], 'outputs': [], 'eliminated': {}, 'disbanded': {},
            'concede': None, 'close': None, 'records': 0, 'escaped': 0,
        })
        for rec in recs:
            squad = per[rec['key']]
            squad['records'] += 1
            squad['escaped'] += 1 if rec['directives'] else 0
            if rec['kind'] == 1:
                squad['deploys'].append((rec['tick'], rec['unit'], rec['value']))
            elif rec['kind'] == 2:
                squad['outputs'].append((rec['tick'], rec['unit'], rec['value']))
            elif rec['kind'] == 3:
                cur = squad['eliminated'].get(rec['unit'])
                squad['eliminated'][rec['unit']] = rec['tick'] if cur is None else min(cur, rec['tick'])
            elif rec['kind'] == 4:
                cur = squad['disbanded'].get(rec['unit'])
                squad['disbanded'][rec['unit']] = rec['tick'] if cur is None else min(cur, rec['tick'])
            elif rec['kind'] == 5:
                squad['concede'] = (rec['tick'] if squad['concede'] is None
                                    else min(squad['concede'], rec['tick']))
            elif rec['kind'] == 8:
                squad['close'] = (rec['tick'] if squad['close'] is None
                                  else max(squad['close'], rec['tick']))

        closing = max([s['close'] or 0 for s in per.values()] or [0])
        squads = {}
        for key, s in per.items():
            gross = voided = 0
            for tick, unit, value in s['outputs']:
                eliminated_at = s['eliminated'].get(unit)
                withdrawn = (eliminated_at is not None and tick >= eliminated_at) or \
                            (s['concede'] is not None and tick >= s['concede'])
                if withdrawn:
                    voided += value
                else:
                    gross += value
            raw = 0
            for tick, unit, rate in s['deploys']:
                end = s['disbanded'].get(unit, closing)
                raw += max(0, end - tick) * rate
            squads[key] = {
                'gross': gross, 'voided': voided, 'raw': raw,
                'concede': s['concede'], 'records': s['records'],
                'escaped': s['escaped'],
            }
        matches.append({'index': index, 'match_id': recs[0]['match_id'], 'squads': squads})
    return matches


def settle_season(records, dictionary, static, budget, threshold):
    """Score, tier and rank every squad of a capture."""
    matches = settle_matches(records)
    owner, sweeps, handovers, cycles = resolve_owners(records, dictionary)
    registry, divisions, licences = (
        static['registry'], static['divisions'], static['licences'])

    def ladder_weight(key):
        return divisions[registry[key]['division']][1] if key in registry else 1

    def levy_rate(key):
        return licences[registry[key]['licence_class']][1] if key in registry else 1

    squads = defaultdict(lambda: {
        'gross': 0, 'voided': 0, 'raw': 0, 'charged': 0, 'overflow': 0,
        'matches': 0, 'score': 0, 'awards': 0, 'streak': 0, 'results': [],
    })
    for match in matches:
        for key, stat in match['squads'].items():
            sq = squads[key]
            sq['gross'] += stat['gross']
            sq['voided'] += stat['voided']
            sq['raw'] += stat['raw']
            sq['matches'] += 1
            room = budget - sq['charged']
            charged = max(0, min(room, stat['raw']))
            sq['charged'] += charged
            sq['overflow'] += stat['raw'] - charged
            stat['charged'] = charged
            stat['score'] = stat['gross'] * ladder_weight(key) - charged * levy_rate(key)
            sq['score'] += stat['score']

        contenders = [k for k, v in match['squads'].items() if v['concede'] is None]
        points = {k: 0 for k in match['squads']}
        outright = None
        if contenders:
            top = max(match['squads'][k]['score'] for k in contenders)
            holders = [k for k in contenders if match['squads'][k]['score'] == top]
            if len(holders) == 1:
                points[holders[0]] = 3
                outright = holders[0]
            else:
                for k in holders:
                    points[k] = 1
        for key, value in points.items():
            squads[key]['awards'] += value
        for key in match['squads']:
            squads[key]['results'].append({
                'match_id': match['match_id'], 'outright': key == outright,
            })

    for key, sq in squads.items():
        run = 0
        for result in sq['results']:
            if result['outright']:
                run += 1
                sq['streak'] += 2 * (run - 1)
            else:
                run = 0
        sq['standing'] = sq['awards'] + sq['streak']
        score = sq['score']
        if score >= 3 * threshold:
            sq['tier'] = 'champion'
        elif score >= 2 * threshold:
            sq['tier'] = 'contender'
        elif score >= threshold:
            sq['tier'] = 'qualifier'
        else:
            sq['tier'] = 'provisional'

    return {
        'matches': matches, 'squads': dict(squads), 'owner': owner, 'sweeps': sweeps,
        'handovers': handovers, 'cycles': cycles,
    }


# --------------------------------------------------------------------------
# Control figures
# --------------------------------------------------------------------------

def control_figures(decoding, settled, tier_names):
    """Map every published control figure label to the value this reading gives."""
    records = decoding['records']
    squads = settled['squads']
    values = {
        'matches opened': len(decoding['logical']),
        'records recovered': len(records),
        'distinct squads': len({r['key'] for r in records}),
        'gross output': sum(s['gross'] for s in squads.values()),
        'voided output': sum(s['voided'] for s in squads.values()),
        'raw upkeep': sum(s['raw'] for s in squads.values()),
        'charged upkeep': sum(s['charged'] for s in squads.values()),
        'overflow upkeep': sum(s['overflow'] for s in squads.values()),
    }
    for tier in tier_names:
        values['%s entrants' % tier] = sum(1 for s in squads.values() if s['tier'] == tier)
    return values


FIGURE_STAGE = {
    'matches opened': 'guards', 'records recovered': 'guards',
    'distinct squads': 'guards', 'gross output': 'guards',
    'voided output': 'guards', 'raw upkeep': 'guards',
    'charged upkeep': 'budget', 'overflow upkeep': 'budget',
}


def stage_of(label, tier_names):
    if label in FIGURE_STAGE:
        return FIGURE_STAGE[label]
    if label.endswith('entrants'):
        return 'threshold'
    return 'unknown'


# --------------------------------------------------------------------------
# Policy search
# --------------------------------------------------------------------------

def search_guard_quantities(captures, static):
    """Recover engine tag, sentinel and window depth against the control slice."""
    tiers = static['tier_names']
    stages = {fid: stage_of(label, tiers) for fid, label, _ in static['calibration']}
    wanted = [(fid, label, value) for fid, label, value in static['calibration']
              if stages[fid] == 'guards']

    audits = {name: [] for name in ('engine_tag', 'window_depth', 'sentinel')}
    surviving = []
    attempts = {}

    for tag in static['engine_tags']:
        for sentinel in static['sentinels']:
            for window in static['window_depths']:
                combo = (tag, sentinel, window)
                reason = None
                figures = None
                try:
                    reference = decode_capture(captures['reference'], tag, sentinel, window)
                    dictionary = build_dictionary(reference)
                    if not dictionary:
                        raise DecodeError('the reference capture names no squad')
                    reference = decode_capture(captures['reference'], tag, sentinel,
                                               window, dictionary)
                    control = decode_capture(captures['control'], tag, sentinel,
                                             window, dictionary)
                    settled = settle_season(control['records'], dictionary, static,
                                            0, max(static['thresholds']))
                    figures = control_figures(control, settled, tiers)
                except (DecodeError, ValueError, IndexError, KeyError, ZeroDivisionError) as exc:
                    reason = '%s' % exc
                failures = []
                if reason is None:
                    for fid, label, published in wanted:
                        got = figures.get(label)
                        if got != published:
                            failures.append({'figure': fid, 'label': label,
                                             'published': published, 'reproduced': got,
                                             'note': None})
                else:
                    # A reading that does not hold recovers nothing at all, so
                    # it fails the first guard level figure outright.
                    fid, label, published = wanted[0]
                    failures.append({'figure': fid, 'label': label,
                                     'published': published, 'reproduced': None,
                                     'note': reason})
                attempts[combo] = {'failures': failures, 'figures': figures}
                if reason is None and not failures:
                    surviving.append(combo)

    for name, values, key in [
        ('engine_tag', static['engine_tags'], 0),
        ('sentinel', static['sentinels'], 1),
        ('window_depth', static['window_depths'], 2),
    ]:
        for value in values:
            rows = [attempts[c] for c in attempts if c[key] == value]
            good = value in [c[key] for c in surviving]
            # Report the falsifier from the reading that got furthest: prefer
            # one that decoded and was beaten by a published figure, and fall
            # back to the decode fault only when no reading held together.
            pool = [r for r in rows if r['figures'] is not None] or rows
            best_row = min(pool, key=lambda r: len(r['failures']))
            first = best_row['failures'][0] if best_row['failures'] else None
            audits[name].append({
                'value': value,
                'verdict': 'accepted' if good else 'rejected',
                'falsifier': None if good else
                             '%s %s' % (first['figure'], first['label']),
                'falsifier_detail': first,
            })

    if len({(c[0], c[1], c[2]) for c in surviving}) != 1:
        raise ValueError('the control figures admit %d readings of the guard quantities'
                         % len(surviving))
    return surviving[0], audits, attempts


def search_policy_figures(captures, static, guard_combo):
    """Recover the season budget and the promotion threshold."""
    tag, sentinel, window = guard_combo
    tiers = static['tier_names']
    dictionary = guard_combo and build_dictionary(
        decode_capture(captures['reference'], tag, sentinel, window))
    control = decode_capture(captures['control'], tag, sentinel, window, dictionary)
    stages = {fid: stage_of(label, tiers) for fid, label, _ in static['calibration']}
    base = {label: value for fid, label, value in static['calibration']}

    results = {}
    for budget in static['budgets']:
        settled = settle_season(control['records'], dictionary, static, budget,
                                max(static['thresholds']))
        figures = control_figures(control, settled, tiers)
        failures = [{'figure': fid, 'label': label, 'published': base[label],
                     'reproduced': figures.get(label), 'note': None}
                    for fid, label, _ in static['calibration']
                    if stages[fid] in ('guards', 'budget')
                    and figures.get(label) != base[label]]
        results[budget] = {'failed': failures, 'ok': not failures,
                           'figures': figures}

    budget_audit = []
    live_budgets = [b for b in static['budgets'] if results[b]['ok']]
    if not live_budgets:
        raise ValueError('no season budget reproduces the control figures')
    budget = live_budgets[0]
    for value in static['budgets']:
        failed = results[value]['failed']
        budget_audit.append({
            'value': value,
            'verdict': 'accepted' if results[value]['ok'] else 'rejected',
            'falsifier': '%s %s' % (failed[0]['figure'], failed[0]['label']) if failed else None,
            'falsifier_detail': failed[0] if failed else None,
        })

    threshold_audit = []
    live = []
    for threshold in static['thresholds']:
        settled = settle_season(control['records'], dictionary, static, budget, threshold)
        figures = control_figures(control, settled, tiers)
        failures = [{'figure': fid, 'label': label, 'published': base[label],
                     'reproduced': figures.get(label), 'note': None}
                    for fid, label, _ in static['calibration']
                    if stages[fid] != 'unknown' and figures.get(label) != base[label]]
        threshold_audit.append({
            'value': threshold,
            'verdict': 'accepted' if not failures else 'rejected',
            'falsifier': '%s %s' % (failures[0]['figure'], failures[0]['label']) if failures else None,
            'falsifier_detail': failures[0] if failures else None,
        })
        if not failures:
            live.append(threshold)
    if not live:
        raise ValueError('no promotion threshold reproduces the control figures')
    return budget, live[0], budget_audit, threshold_audit


# --------------------------------------------------------------------------
# Bracket and docket
# --------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    ('prominence', 'at least one member sits in a division whose ladder weight reaches the prominence figure'),
    ('segments', 'both entrant segments appear among the members'),
    ('standing_floor', 'every member holds at least the standing points floor'),
    ('conferences', 'no two members share a conference'),
    ('ranked', 'at least one member is a ranked entrant'),
    ('weight_floor', 'the ladder weights of the members sum to at least the combined ladder weight floor'),
    ('no_overflow', 'no member carries any overflow upkeep'),
    ('champion', 'at least one member holds the champion tier'),
    ('gross_floor', 'the gross output of the members sums to at least the combined gross output floor'),
    ('no_handover', 'no member has handed its entry to another squad'),
]


def elect_bracket(settled, static):
    """Choose the three squad finals bracket that satisfies the most constraints."""
    registry, divisions, licences = (
        static['registry'], static['divisions'], static['licences'])
    squads = settled['squads']
    owner, handovers = settled['owner'], settled['handovers']

    def conference(key):
        return divisions[registry[key]['division']][0] if key in registry else None

    def segment(key):
        return licences[registry[key]['licence_class']][0] if key in registry else None

    def ladder_weight(key):
        return divisions[registry[key]['division']][1] if key in registry else 1

    eligible = [k for k, s in squads.items()
                if owner.get(k, k) == k and s['matches'] >= static['eligibility_floor']]
    eligible.sort()

    best = None
    space = 0
    for combo in combinations(eligible, 3):
        space += 1
        keys = sorted(combo)
        satisfied = []
        if any(ladder_weight(k) >= static['prominence'] for k in keys):
            satisfied.append('prominence')
        if all(seg in {segment(k) for k in keys} for seg in static['segments']) \
                and len(static['segments']) > 0:
            satisfied.append('segments')
        if all(squads[k]['standing'] >= static['standing_floor'] for k in keys):
            satisfied.append('standing_floor')
        confs = [conference(k) for k in keys]
        if len(set(confs)) == len(confs):
            satisfied.append('conferences')
        if any(registry.get(k, {}).get('is_ranked', False) for k in keys):
            satisfied.append('ranked')
        if sum(ladder_weight(k) for k in keys) >= static['weight_floor']:
            satisfied.append('weight_floor')
        if all(squads[k]['overflow'] == 0 for k in keys):
            satisfied.append('no_overflow')
        if any(squads[k]['tier'] == 'champion' for k in keys):
            satisfied.append('champion')
        if sum(squads[k]['gross'] for k in keys) >= static['gross_floor']:
            satisfied.append('gross_floor')
        if all(k not in handovers for k in keys):
            satisfied.append('no_handover')
        upkeep = sum(squads[k]['charged'] for k in keys)
        # Most constraints met, then least charged upkeep in total, then the
        # bracket whose member keys sort first.
        rank = (len(satisfied), -upkeep)
        if best is None or rank > best[0] or (rank == best[0] and keys < best[1]):
            best = (rank, keys, satisfied, upkeep)

    if best is None:
        return {'members': [], 'satisfied': [], 'candidate_space': 0,
                'best_satisfied': 0, 'charged_upkeep_total': 0}
    _, keys, satisfied, upkeep = best
    return {
        'members': keys,
        'satisfied': satisfied,
        'unsatisfied': [name for name, _ in BRACKET_CONSTRAINTS if name not in satisfied],
        'candidate_space': space,
        'best_satisfied': len(satisfied),
        'charged_upkeep_total': upkeep,
        'eligible_squads': eligible,
    }


def elect_docket(settled, static):
    """Choose the docket of matches with the largest total review index."""
    registry, divisions = static['registry'], static['divisions']
    size, cap, weight = (static['docket_size'], static['docket_cap'],
                         static['review_weight'])

    conferences = defaultdict(list)   # conference -> match ids
    match_rows = []
    for match in settled['matches']:
        registered = [k for k in match['squads'] if k in registry]
        if registered:
            top = sorted(registered, key=lambda k: (-match['squads'][k]['gross'], k))[0]
            conf = divisions[registry[top]['division']][0]
        else:
            conf = None
        index = sum(v['voided'] for v in match['squads'].values()) + \
            weight * sum(v['escaped'] for v in match['squads'].values())
        match_rows.append({'match_id': match['match_id'], 'conference': conf,
                           'review_index': index})
        conferences[conf].append(match['match_id'])

    by_id = {r['match_id']: r for r in match_rows}
    ids = sorted(by_id)
    if len(ids) < size:
        raise ValueError('the season holds fewer matches than the docket size')

    best = None
    for combo in combinations(ids, size):
        tally = defaultdict(int)
        for mid in combo:
            tally[by_id[mid]['conference']] += 1
        if any(v > cap for k, v in tally.items() if k is not None):
            continue
        total = sum(by_id[mid]['review_index'] for mid in combo)
        key = (total, combo)
        if best is None or key[0] > best[0] or (key[0] == best[0] and combo < best[1]):
            best = (total, combo)

    chosen = list(best[1])
    spread = defaultdict(int)
    for mid in chosen:
        spread[by_id[mid]['conference']] += 1
    return {
        'matches': [{'match_id': mid, 'conference': by_id[mid]['conference'],
                     'review_index': by_id[mid]['review_index']} for mid in chosen],
        'total_review_index': best[0],
        'conference_spread': dict(sorted(spread.items(), key=lambda kv: str(kv[0]))),
        'per_match': {r['match_id']: r['review_index'] for r in match_rows},
    }


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def group_report(kind, name, members, settled, static, tier_names):
    divisions, licences, registry = (
        static['divisions'], static['licences'], static['registry'])
    entries = []
    for key in sorted(members):
        sq = settled['squads'][key]
        reg = registry.get(key)
        entries.append({
            'charged_upkeep': sq['charged'],
            'conference': divisions[reg['division']][0] if reg else None,
            'division': reg['division'] if reg else None,
            'entrant_id': reg['entrant_id'] if reg else None,
            'gross_output': sq['gross'],
            'licence_class': reg['licence_class'] if reg else None,
            'matches': sq['matches'],
            'overflow_upkeep': sq['overflow'],
            'season_score': sq['score'],
            'segment': licences[reg['licence_class']][0] if reg else None,
            'squad_key': key,
            'standing_points': sq['standing'],
            'streak_credit': sq['streak'],
            'tier': sq['tier'],
            'voided_output': sq['voided'],
        })
    tier_counts = {tier: 0 for tier in tier_names}
    for entry in entries:
        tier_counts[entry['tier']] += 1
    divs = sorted({e['division'] for e in entries if e['division']})
    return {
        'charged_upkeep': sum(e['charged_upkeep'] for e in entries),
        'divisions': divs,
        'entrants': len(entries),
        'entries': entries,
        'gross_output': sum(e['gross_output'] for e in entries),
        'group_kind': kind,
        'group_name': name,
        'overflow_upkeep': sum(e['overflow_upkeep'] for e in entries),
        'season_score': sum(e['season_score'] for e in entries),
        'standing_points': sum(e['standing_points'] for e in entries),
        'tier_counts': tier_counts,
        'voided_output': sum(e['voided_output'] for e in entries),
    }


def write_json(path, payload):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write('\n')


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    static = load_static(data_dir)

    captures = {}
    capture_meta = {}
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith('.rjnl'):
            continue
        cap = Capture(os.path.join(data_dir, name))
        role = ROLE_BY_CAPID_SUFFIX.get(cap.capture_id % 10)
        if role and role not in captures:
            captures[role] = cap
            capture_meta[role] = name
    missing = [r for r in ('reference', 'control', 'season') if r not in captures]
    if missing:
        raise ValueError('captures missing from %s: %s' % (data_dir, ', '.join(missing)))

    # -- recover the unstated quantities against the control slice ----------
    guard_combo, guard_audit, _attempts = search_guard_quantities(captures, static)
    engine_tag, sentinel, window = guard_combo
    budget, threshold, budget_audit, threshold_audit = search_policy_figures(
        captures, static, guard_combo)

    dictionary = build_dictionary(
        decode_capture(captures['reference'], engine_tag, sentinel, window))

    decoded = {}
    for role in ('reference', 'control', 'season'):
        decoded[role] = decode_capture(captures[role], engine_tag, sentinel,
                                       window, dictionary)

    settled = settle_season(decoded['season']['records'], dictionary, static,
                            budget, threshold)
    control_settled = settle_season(decoded['control']['records'], dictionary, static,
                                    budget, threshold)

    registry, divisions, licences = static['registry'], static['divisions'], static['licences']
    squads = settled['squads']
    present = set(squads)
    unregistered = sorted(present - set(registry))
    absent = sorted(set(registry) - present)

    # -- calibration table --------------------------------------------------
    tiers = static['tier_names']
    reproduced = control_figures(decoded['control'], control_settled, tiers)
    calibration = []
    for fid, label, published in static['calibration']:
        got = reproduced.get(label)
        calibration.append({
            'figure': fid, 'label': label, 'published': published,
            'reproduced': got, 'agree': got == published,
        })

    # -- group reports ------------------------------------------------------
    season_keys = sorted(present)
    for conference in static['conferences']:
        members = [k for k in season_keys
                   if k in registry and divisions[registry[k]['division']][0] == conference]
        write_json(os.path.join(out_dir, 'region_%s.json' % conference),
                   group_report('conference', conference, members, settled, static, tiers))
    for segment in static['segments']:
        members = [k for k in season_keys
                   if k in registry and licences[registry[k]['licence_class']][0] == segment]
        write_json(os.path.join(out_dir, 'segment_%s.json' % segment),
                   group_report('segment', segment, members, settled, static, tiers))

    # -- data quality -------------------------------------------------------
    quality = {'captures': [], 'season': {}, 'reference_roster_dictionary': dictionary}
    for role in ('reference', 'control', 'season'):
        cap, dec = captures[role], decoded[role]
        keys = {r['key'] for r in dec['records']}
        registered = keys & set(registry)
        quality['captures'].append({
            'role': role,
            'file': capture_meta[role],
            'capture_id': cap.capture_id,
            'guard_register': cap.guard_register,
            'guards_active': cap.active_guards,
            'count_bias_applied': dec['bias'],
            'envelope_checksum_ok': cap.envelope_checksum_ok,
            'segment_payload_checksums_ok': all(s['checksum_ok'] for s in cap.segments),
            'declared_segment_count': dec['declared_segments'],
            'recovered_segment_count': len(dec['logical']),
            'segment_count_residual': dec['declared_segments'] - len(dec['logical']),
            'declared_record_count': dec['declared_records'],
            'recovered_record_count': len(dec['records']),
            'record_count_residual': dec['declared_records'] - len(dec['records']),
            'declared_roster_size': dec['declared_roster'],
            'registered_squads_recovered': len(registered),
            'distinct_squads_recovered': len(keys),
            'roster_size_residual': dec['declared_roster'] - len(registered),
        })

    conference_gross = 0
    for conference in static['conferences']:
        members = [k for k in season_keys
                   if k in registry and divisions[registry[k]['division']][0] == conference]
        conference_gross += sum(squads[k]['gross'] for k in members)
    season_gross = sum(s['gross'] for s in squads.values())
    reported_output = sum(r['value'] for r in decoded['season']['records'] if r['kind'] == 2)
    gross_total = sum(s['gross'] for s in squads.values())
    void_total = sum(s['voided'] for s in squads.values())
    raw_total = sum(s['raw'] for s in squads.values())
    charged_total = sum(s['charged'] for s in squads.values())
    overflow_total = sum(s['overflow'] for s in squads.values())
    ledger = {name: 0 for _, name in KIND_LEDGER}
    for rec in decoded['season']['records']:
        ledger[KIND_NAMES[rec['kind']]] += 1

    quality['season'] = {
        'gross_output': gross_total,
        'conference_gross_output_sum': conference_gross,
        'attribution_residual': gross_total - conference_gross,
        'output_ledger': {
            'reported_output': reported_output,
            'gross_output': gross_total,
            'voided_output': void_total,
            'residual': reported_output - gross_total - void_total,
        },
        'upkeep_ledger': {
            'raw_upkeep': raw_total,
            'charged_upkeep': charged_total,
            'overflow_upkeep': overflow_total,
            'residual': raw_total - charged_total - overflow_total,
        },
        'backwards_tick_transitions': decoded['season']['backwards_ticks'],
        'kind_ledger': ledger,
    }
    quality['baseline_transcript_reconciliation'] = reconcile_transcript(
        data_dir, decoded['reference'])
    quality['unregistered_squads'] = unregistered
    quality['unregistered_squad_output'] = {
        k: {'gross_output': squads[k]['gross'], 'voided_output': squads[k]['voided'],
            'raw_upkeep': squads[k]['raw'], 'charged_upkeep': squads[k]['charged'],
            'overflow_upkeep': squads[k]['overflow'], 'matches': squads[k]['matches'],
            'season_score': squads[k]['score']}
        for k in unregistered}
    quality['absent_entrants'] = [
        {'squad_key': k, 'entrant_id': registry[k]['entrant_id'],
         'division': registry[k]['division'], 'licence_class': registry[k]['licence_class']}
        for k in absent]
    write_json(os.path.join(out_dir, 'data_quality.json'), quality)

    # -- policy audit -------------------------------------------------------
    audit = {
        'method': (
            'Every unstated quantity is drawn from its published roster and tested '
            'against the control figures published with the control slice. A reading '
            'that reproduces every published figure is accepted; every other '
            'candidate carries the control figure that killed it.'),
        'unstated_quantities': [
            {'name': 'engine_tag',
             'drawn_from': 'the engine tag roster published in replay-guards.md',
             'candidate_space': static['engine_tags'],
             'candidates': guard_audit['engine_tag']},
            {'name': 'window_depth',
             'drawn_from': 'the window depth roster published in replay-guards.md',
             'candidate_space': static['window_depths'],
             'candidates': guard_audit['window_depth']},
            {'name': 'sentinel',
             'drawn_from': 'the sentinel roster published in replay-guards.md',
             'candidate_space': ['0x%02X' % v for v in static['sentinels']],
             'candidates': [{'value': '0x%02X' % c['value'],
                             'verdict': c['verdict'],
                             'falsifier': c['falsifier'],
                             'falsifier_detail': c['falsifier_detail']}
                            for c in guard_audit['sentinel']]},
            {'name': 'season_budget',
             'drawn_from': 'the budget cap roster published in upkeep-policy.md',
             'candidate_space': static['budgets'],
             'candidates': budget_audit},
            {'name': 'promotion_threshold',
             'drawn_from': 'the promotion threshold roster published in standings-policy.md',
             'candidate_space': static['thresholds'],
             'candidates': threshold_audit},
        ],
        'resolved': {
            'engine_tag': engine_tag,
            'window_depth': window,
            'sentinel': '0x%02X' % sentinel,
            'season_budget': budget,
            'promotion_threshold': threshold,
        },
        'attribution_settlement': {
            'sweeps': settled['sweeps'],
            'handovers': {k: settled['handovers'][k] for k in sorted(settled['handovers'])},
            'closed_sets': settled['cycles'],
            'effective_owners': {k: settled['owner'][k] for k in sorted(settled['owner'])
                                 if settled['owner'][k] != k},
        },
        'bracket': None,
        'calibration': calibration,
        'calibration_all_agree': all(row['agree'] for row in calibration),
    }

    bracket = elect_bracket(settled, static)
    docket = elect_docket(settled, static)
    audit['bracket'] = {
        'candidate_space': bracket['candidate_space'],
        'best_satisfied_constraint_count': bracket['best_satisfied'],
        'constraints': [{'name': name, 'text': text,
                         'satisfied': name in bracket['satisfied']}
                        for name, text in BRACKET_CONSTRAINTS],
        'elected_members': bracket['members'],
        'tie_break': ('fewest charged upkeep in total, then the bracket whose member '
                      'keys sort first'),
    }
    write_json(os.path.join(out_dir, 'policy_audit.json'), audit)

    # -- summary ------------------------------------------------------------
    ladder = sorted(season_keys, key=lambda k: (-squads[k]['standing'],
                                                -squads[k]['score'], k))
    summary = {
        'policies_in_force': {
            'engine_tag': engine_tag,
            'window_depth': window,
            'sentinel': '0x%02X' % sentinel,
            'season_budget': budget,
            'promotion_threshold': threshold,
            'eligibility_match_floor': static['eligibility_floor'],
            'review_weight': static['review_weight'],
            'docket_size': static['docket_size'],
            'docket_conference_cap': static['docket_cap'],
        },
        'captures': {
            role: {'file': capture_meta[role],
                   'capture_id': captures[role].capture_id,
                   'guard_register': captures[role].guard_register,
                   'matches': len(decoded[role]['logical']),
                   'records': len(decoded[role]['records'])}
            for role in ('reference', 'control', 'season')},
        'season': {
            'matches': len(settled['matches']),
            'records': len(decoded['season']['records']),
            'squads': len(squads),
            'registered_squads': len(present & set(registry)),
            'unregistered_squads': len(unregistered),
            'absent_entrants': len(absent),
            'gross_output': gross_total,
            'voided_output': void_total,
            'raw_upkeep': raw_total,
            'charged_upkeep': charged_total,
            'overflow_upkeep': overflow_total,
        },
        'tier_counts': {tier: sum(1 for s in squads.values() if s['tier'] == tier)
                        for tier in tiers},
        'attribution_settlement': {
            'sweeps': settled['sweeps'],
            'handovers_recorded': len(settled['handovers']),
            'closed_sets': settled['cycles'],
            'squads_not_own_effective_owner': sorted(
                k for k in settled['owner'] if settled['owner'][k] != k),
        },
        'ladder': [
            {'rank': i + 1, 'squad_key': k,
             'standing_points': squads[k]['standing'],
             'match_awards': squads[k]['awards'],
             'streak_credit': squads[k]['streak'],
             'season_score': squads[k]['score'], 'tier': squads[k]['tier'],
             'gross_output': squads[k]['gross'], 'voided_output': squads[k]['voided'],
             'charged_upkeep': squads[k]['charged'],
             'overflow_upkeep': squads[k]['overflow'],
             'matches': squads[k]['matches'],
             'registered': k in registry}
            for i, k in enumerate(ladder)],
        'finals_bracket': {
            'members': bracket['members'],
            'satisfied_constraints': bracket['satisfied'],
            'satisfied_constraint_count': bracket['best_satisfied'],
            'candidate_space': bracket['candidate_space'],
            'charged_upkeep_total': bracket['charged_upkeep_total'],
        },
        'review_docket': {
            'matches': [m['match_id'] for m in docket['matches']],
            'total_review_index': docket['total_review_index'],
            'conference_spread': docket['conference_spread'],
            'entries': docket['matches'],
        },
    }
    write_json(os.path.join(out_dir, 'summary.json'), summary)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
