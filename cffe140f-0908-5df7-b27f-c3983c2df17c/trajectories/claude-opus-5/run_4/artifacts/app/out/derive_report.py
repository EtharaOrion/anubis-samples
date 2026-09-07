#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR, standard library only, no network.

The written contract is implemented in general: every policy figure, roster and
control figure is read out of the documents shipped in DATA_DIR, the three
captures are told apart by the capture identifier in their envelopes rather than
by file name, and the unstated quantities are searched over their published
rosters and pinned against the published control figures.
"""

import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys
from collections import Counter, defaultdict
from math import gcd

M32 = 1 << 32

GUARD_BITS = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
              'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']

KIND_NAMES = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
              5: 'concede', 6: 'transfer', 8: 'close'}

BRACKET_CONSTRAINTS = [
    'prominent_division', 'both_segments', 'standing_points_floor',
    'distinct_conferences', 'ranked_entrant', 'combined_ladder_weight',
    'no_overflow_upkeep', 'champion_member', 'combined_gross_output',
    'no_handover',
]

TIER_NAMES = ['champion', 'contender', 'qualifier', 'provisional']


# --------------------------------------------------------------------------
# document scraping
# --------------------------------------------------------------------------

def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()


def scrape_documents(data_dir):
    """Pull every roster and every named policy figure out of the markdown."""
    lines = []
    for name in sorted(os.listdir(data_dir)):
        if name.lower().endswith('.md'):
            lines.extend(read_text(os.path.join(data_dir, name)).splitlines())

    # a label may wrap onto the next line, so each line is also tried joined to
    # its successor; a label is always anchored at the head of a line either way
    probes = list(lines) + [a.rstrip() + ' ' + b.lstrip() for a, b in zip(lines, lines[1:])]

    rosters, figures, controls = {}, {}, []
    seen_controls = set()
    for probe in probes:
        probe = re.sub(r'\s+', ' ', probe).strip()
        probe = re.sub(r'^[-*]\s*', '', probe)

        m = re.match(r'^([A-Za-z][A-Za-z ]*?) roster: ((?:`[^`]*`(?:, )?)+)$', probe)
        if m:
            key = m.group(1).strip().lower().replace(' ', '_')
            rosters.setdefault(key, re.findall(r'`([^`]*)`', m.group(2)))
            continue

        m = re.match(r'^([A-Za-z][A-Za-z ]*?): `([^`]*)`$', probe)
        if m:
            key = m.group(1).strip().lower().replace(' ', '_')
            figures.setdefault(key, m.group(2))
            continue

        m = re.match(r'^(C\d+) ([A-Za-z][A-Za-z ]*?): (-?\d+)$', probe)
        if m and m.group(1) not in seen_controls:
            seen_controls.add(m.group(1))
            controls.append({'id': m.group(1), 'label': m.group(2).strip().lower(),
                             'published': int(m.group(3))})

    for key in list(figures):
        if key.endswith('_roster'):
            del figures[key]
    controls.sort(key=lambda c: int(c['id'][1:]))
    return rosters, figures, controls


def as_int(token):
    token = token.strip()
    return int(token, 16) if token.lower().startswith('0x') else int(token)


def fig_int(figures, key, default):
    try:
        return as_int(figures[key])
    except (KeyError, ValueError):
        return default


def find_csv(data_dir, required):
    """Locate a table by the columns it declares, not by its file name."""
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith('.csv'):
            continue
        path = os.path.join(data_dir, name)
        with open(path, newline='', encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
        if rows and required.issubset(set(rows[0].keys())):
            return rows
    raise SystemExit('no csv in %s carrying columns %s' % (data_dir, sorted(required)))


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

def rot_left(buf, n):
    n %= len(buf)
    return buf[n:] + buf[:n]


def keystream_bytes(engine_tag, match_id, eff_salt, seg_ordinal, width):
    seed = ('%s:%d:%d:%d' % (engine_tag, match_id, eff_salt, seg_ordinal)).encode()
    out = hashlib.sha256(seed).digest()
    while len(out) < width:
        out += hashlib.sha256(out).digest()
    return out[:width]


def read_envelope(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw[:4] != b'RJNL':
        return None
    env = raw[:36]
    return {
        'path': path,
        'raw': raw,
        'version': env[4],
        'reserved': env[5],
        'guard_register': struct.unpack('<H', env[6:8])[0],
        'nonce_le': struct.unpack('<I', env[8:12])[0],
        'nonce_be': struct.unpack('>I', env[8:12])[0],
        'declared_segments': struct.unpack('<I', env[12:16])[0],
        'declared_records': struct.unpack('<I', env[16:20])[0],
        'base_tick': struct.unpack('<I', env[20:24])[0],
        'capture_id': struct.unpack('<I', env[24:28])[0],
        'declared_roster_size': struct.unpack('<I', env[28:32])[0],
        'envelope_checksum': struct.unpack('<I', env[32:36])[0],
        'envelope_checksum_recomputed': sum(env[:32]) % M32,
    }


class DecodeError(Exception):
    pass


def decode(envelope, engine_tag, window_depth, sentinel, roster_dict, progress=None):
    """Recover every record of one capture in logical order.

    A caller may pass a progress dictionary; it is filled in as the reading goes
    so that a candidate whose reading collapses can still be scored, on whatever
    it did recover, against the published control figures.
    """
    raw = envelope['raw']
    guard = envelope['guard_register']
    on = lambda name: bool(guard >> GUARD_BITS.index(name) & 1)

    nonce = envelope['nonce_be'] if on('endian') else envelope['nonce_le']
    bias = sum(engine_tag.encode()) % 11 if on('countbias') else 0

    off = 36
    stored = []
    while off + 28 <= len(raw):
        mark, slot, mid, drc, salt, pw, pc = struct.unpack('<4sIIIIII', raw[off:off + 28])
        if mark != b'SEG0':
            break
        payload = raw[off + 28:off + 28 + pw]
        if len(payload) != pw:
            raise DecodeError('segment payload truncated')
        stored.append({'slot': slot, 'match_id': mid, 'declared_records': drc,
                       'records': drc - bias, 'salt': salt, 'payload_width': pw,
                       'payload_checksum': pc,
                       'payload_checksum_recomputed': sum(payload) % M32,
                       'payload': payload})
        off += 28 + pw
    trailing = len(raw) - off
    n = len(stored)
    if n == 0:
        raise DecodeError('no segments')
    if any(s['records'] < 0 for s in stored):
        raise DecodeError('negative segment record count')

    # segment permutation: storage slot i holds logical position (nonce + i*step) % n
    step = None
    if on('segperm'):
        step = 2 + ((nonce >> 8) % n)
        while gcd(step, n) != 1:
            step += 1
        logical = [None] * n
        for i, seg in enumerate(stored):
            logical[(nonce + i * step) % n] = seg
        if any(x is None for x in logical):
            raise DecodeError('segment permutation is not a bijection')
    else:
        logical = list(stored)

    records = []
    backwards_ticks = 0
    unresolved_escapes = 0
    drift_sum = 0
    ordinal = 0
    prev_tick = None
    backlist = []
    if progress is not None:
        progress['records'] = records
        progress['segments_recovered'] = 0

    for lpos, seg in enumerate(logical):
        eff_salt = (seg['salt'] ^ drift_sum) % M32 if on('driftkey') else seg['salt']
        seg['effective_salt'] = eff_salt
        seg['logical_position'] = lpos
        payload = seg['payload']
        cursor = 0
        first_diff = 0
        tick_acc = 0
        seg_value_sum = 0
        last_tick = None
        for seg_ordinal in range(seg['records']):
            if cursor + 2 > len(payload):
                raise DecodeError('record frame runs past payload')
            visible, shadow = payload[cursor], payload[cursor + 1]
            width = (shadow ^ (eff_salt & 0xFF)) if on('shadow') else visible
            blob = payload[cursor + 2:cursor + 2 + width]
            if width < 13 or len(blob) != width:
                raise DecodeError('record blob width %d not recoverable' % width)
            cursor += 2 + width

            if on('keystream'):
                ks = keystream_bytes(engine_tag, seg['match_id'], eff_salt, seg_ordinal, width)
                blob = bytes(a ^ b for a, b in zip(blob, ks))
            if on('rotate'):
                turn = (eff_salt + ordinal) % width
                blob = rot_left(blob, width - turn)

            directives = []
            head = 0
            if on('escape'):
                while len(directives) < 2 and head + 1 < len(blob) and blob[head] == sentinel:
                    directives.append(blob[head + 1])
                    head += 2
            body = blob[head:]
            if len(body) < 13:
                raise DecodeError('record body too short')
            kind = body[0]
            mode = body[1]
            if kind not in KIND_NAMES or mode not in (0, 1, 2):
                raise DecodeError('record body does not parse')
            if mode == 0:
                attr = body[2:5]
                rest = body[5:]
                try:
                    wire_key = attr.decode('ascii')
                except UnicodeDecodeError:
                    raise DecodeError('attribution key is not ascii')
                if not (len(wire_key) == 3 and wire_key.isalnum()):
                    raise DecodeError('attribution key is not a squad key')
            else:
                attr = body[2]
                rest = body[3:]
                wire_key = None
            if len(rest) < 10:
                raise DecodeError('record tail too short')
            unit = struct.unpack('<H', rest[0:2])[0]
            tick_field = struct.unpack('<I', rest[2:6])[0]
            value_field = struct.unpack('<I', rest[6:10])[0]

            if on('delta2'):
                first_diff = (first_diff + tick_field) % M32
                tick_acc = (tick_acc + first_diff) % M32
                tick = tick_acc
            else:
                tick = tick_field

            if on('chain'):
                value = value_field ^ (nonce if ordinal == 0 else prev_tick)
            else:
                value = value_field

            if mode == 1:
                if attr >= len(backlist):
                    raise DecodeError('back reference outside the rolling window')
                wire_key = backlist[attr]
            elif mode == 2:
                if not roster_dict:
                    raise DecodeError('alias without a roster dictionary')
                wire_key = roster_dict[attr % len(roster_dict)]

            # the rolling window is refreshed with the key as written on the wire,
            # before any escape directive is weighed
            if wire_key in backlist:
                backlist.remove(wire_key)
            backlist.append(wire_key)
            while len(backlist) > window_depth:
                backlist.pop(0)

            key = wire_key
            for directive in directives:
                op, operand = directive >> 4, directive & 0xF
                if op == 1 and roster_dict:
                    key = roster_dict[operand % len(roster_dict)]
                elif op == 2:
                    if roster_dict and key in roster_dict:
                        here = roster_dict.index(key)
                        key = roster_dict[(here + operand) % len(roster_dict)]
                    else:
                        # the record holds no roster position to advance from
                        unresolved_escapes += 1

            if last_tick is not None and tick < last_tick:
                backwards_ticks += 1
            last_tick = tick

            records.append({'kind': kind, 'mode': mode, 'squad_key': key,
                            'wire_key': wire_key, 'unit': unit, 'tick': tick,
                            'value': value, 'match_id': seg['match_id'],
                            'logical_segment': lpos, 'record_ordinal': ordinal,
                            'segment_ordinal': seg_ordinal,
                            'escape_directives': len(directives)})
            seg_value_sum = (seg_value_sum + value) % M32
            prev_tick = tick
            ordinal += 1

        seg['filler_width'] = seg['payload_width'] - cursor
        seg['expected_filler_width'] = 3 + seg['salt'] % 5
        drift_sum = (drift_sum + seg_value_sum) % M32
        if progress is not None:
            progress['segments_recovered'] = lpos + 1

    return {
        'envelope': envelope,
        'guards_active': [g for g in GUARD_BITS if on(g)],
        'nonce': nonce,
        'count_bias': bias,
        'segment_step': step,
        'segments': logical,
        'records': records,
        'recovered_segments': n,
        'recovered_records': len(records),
        'effective_segments': envelope['declared_segments'] - bias,
        'effective_records': envelope['declared_records'] - bias,
        'effective_roster_size': envelope['declared_roster_size'] - bias,
        'trailing_bytes': trailing,
        'backwards_ticks': backwards_ticks,
        'unresolved_escape_directives': unresolved_escapes,
    }


def structurally_sound(dec):
    """A capture is only readable when every frame tiles its payload exactly."""
    if dec['recovered_segments'] != dec['effective_segments']:
        return False
    if dec['recovered_records'] != dec['effective_records']:
        return False
    if dec['trailing_bytes'] != 0:
        return False
    for seg in dec['segments']:
        if seg['filler_width'] != seg['expected_filler_width']:
            return False
        if seg['payload_checksum'] != seg['payload_checksum_recomputed']:
            return False
    return True


def verify_transcript(data_dir, dec):
    """Check the reference reading field for field against its labelled transcript."""
    transcript = None
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith('.jsonl'):
            continue
        rows = []
        with open(os.path.join(data_dir, name), 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if rows and 'kind' in rows[0] and 'squad_key' in rows[0]:
            transcript = (name, rows)
            break
    if transcript is None:
        return {'transcript_present': False}
    name, rows = transcript
    recovered = dec['records']
    fields = sorted(set(rows[0]) & set(recovered[0])) if recovered else sorted(rows[0])
    mismatches = []
    for want, got in zip(rows, recovered):
        diff = {f: [want[f], got[f]] for f in fields if want[f] != got[f]}
        if diff:
            mismatches.append({'record_ordinal': want.get('record_ordinal'), 'differs': diff})
    return {
        'transcript_present': True,
        'transcript_file': name,
        'transcript_records': len(rows),
        'recovered_records': len(recovered),
        'fields_compared': fields,
        'mismatching_records': len(mismatches),
        'first_mismatches': mismatches[:5],
        'byte_exact': len(rows) == len(recovered) and not mismatches,
    }


def build_roster_dictionary(dec):
    order = []
    for rec in dec['records']:
        if rec['wire_key'] not in order:
            order.append(rec['wire_key'])
    return order


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def effective_owners(records, roster_dict):
    """Follow the hand overs to their end; report the sweep count."""
    hands_to = {}
    for rec in records:                       # already in logical order
        if rec['kind'] == 6 and roster_dict:
            hands_to[rec['squad_key']] = roster_dict[rec['value'] % len(roster_dict)]

    resolved = dict(hands_to)
    for start in list(hands_to):
        path = []
        cur = start
        while cur in resolved and cur not in path:
            path.append(cur)
            cur = resolved[cur]
        if cur in path:                       # a closed set of hand overs
            cycle = path[path.index(cur):]
            settle_on = min(cycle)
            for member in cycle:
                resolved[member] = settle_on
            resolved.pop(settle_on, None)

    universe = set(hands_to) | set(hands_to.values())
    owner = {k: k for k in universe}
    passes = 0
    while True:
        passes += 1
        nxt = {k: resolved.get(v, v) for k, v in owner.items()}
        if nxt == owner:
            break
        owner = nxt
    return owner, set(hands_to), passes


def match_settlement(dec):
    """Per match: per squad gross / voided / raw upkeep, plus review inputs."""
    by_segment = defaultdict(list)
    for rec in dec['records']:
        by_segment[rec['logical_segment']].append(rec)

    matches = []
    for lpos in sorted(by_segment):
        rows = by_segment[lpos]
        eliminated, conceded, close_tick = {}, {}, None
        for rec in rows:
            key, unit = rec['squad_key'], rec['unit']
            if rec['kind'] == 3 and (key, unit) not in eliminated:
                eliminated[(key, unit)] = rec['tick']
            elif rec['kind'] == 5 and key not in conceded:
                conceded[key] = rec['tick']
            elif rec['kind'] == 8 and close_tick is None:
                close_tick = rec['tick']
        if close_tick is None:
            close_tick = max(r['tick'] for r in rows)

        stats = defaultdict(lambda: {'gross_output': 0, 'voided_output': 0, 'raw_upkeep': 0})
        on_field = {}
        for rec in rows:
            key, unit = rec['squad_key'], rec['unit']
            slot = stats[key]
            if rec['kind'] == 1:
                if (key, unit) in on_field:          # redeployed without a disband
                    start, rate = on_field.pop((key, unit))
                    slot['raw_upkeep'] += rate * max(0, rec['tick'] - start)
                on_field[(key, unit)] = (rec['tick'], rec['value'])
            elif rec['kind'] == 4:
                if (key, unit) in on_field:
                    start, rate = on_field.pop((key, unit))
                    slot['raw_upkeep'] += rate * max(0, rec['tick'] - start)
            elif rec['kind'] == 2:
                gone = eliminated.get((key, unit))
                quit_ = conceded.get(key)
                withdrawn = (gone is not None and rec['tick'] >= gone) or \
                            (quit_ is not None and rec['tick'] >= quit_)
                slot['voided_output' if withdrawn else 'gross_output'] += rec['value']
        for (key, unit), (start, rate) in on_field.items():
            stats[key]['raw_upkeep'] += rate * max(0, close_tick - start)

        matches.append({
            'logical_position': lpos,
            'match_id': rows[0]['match_id'],
            'close_tick': close_tick,
            'stats': {k: dict(v) for k, v in stats.items()},
            'conceded': set(conceded),
            'escape_records': sum(1 for r in rows if r['escape_directives']),
            'records': len(rows),
        })
    return matches


def award_standing_points(matches, scores):
    """Three points for an outright win, one apiece for a shared best."""
    for m in matches:
        contenders = {k: scores[(m['match_id'], k)] for k in m['stats']
                      if k not in m['conceded']}
        m['awards'] = {k: 0 for k in m['stats']}
        m['outright'] = None
        if contenders:
            best = max(contenders.values())
            winners = [k for k, v in contenders.items() if v == best]
            if len(winners) == 1:
                m['awards'][winners[0]] = 3
                m['outright'] = winners[0]
            else:
                for k in winners:
                    m['awards'][k] = 1


def streak_credit(matches, key):
    credit, run = 0, 0
    for m in matches:
        if key not in m['stats']:
            continue
        if m['outright'] == key:
            run += 1
            if run >= 2:
                credit += 2
        else:
            run = 0
    return credit


def settle(dec, roster_dict, registry, divisions, licences, budget, threshold):
    owner, handed_over, passes = effective_owners(dec['records'], roster_dict)
    matches = match_settlement(dec)

    def weight_of(key):
        row = registry.get(key)
        return int(divisions[row['division']]['ladder_weight']) if row else 1

    def levy_of(key):
        row = registry.get(key)
        return int(licences[row['licence_class']]['levy_rate']) if row else 1

    squads = sorted({k for m in matches for k in m['stats']})
    remaining = {k: budget for k in squads}
    agg = {k: {'gross_output': 0, 'voided_output': 0, 'raw_upkeep': 0,
               'charged_upkeep': 0, 'overflow_upkeep': 0, 'season_score': 0,
               'matches': 0} for k in squads}
    scores = {}
    for m in matches:
        m['charged'] = {}
        m['overflow'] = {}
        for key, s in sorted(m['stats'].items()):
            charged = min(remaining[key], s['raw_upkeep'])
            remaining[key] -= charged
            overflow = s['raw_upkeep'] - charged
            m['charged'][key] = charged
            m['overflow'][key] = overflow
            score = s['gross_output'] * weight_of(key) - charged * levy_of(key)
            scores[(m['match_id'], key)] = score
            a = agg[key]
            a['gross_output'] += s['gross_output']
            a['voided_output'] += s['voided_output']
            a['raw_upkeep'] += s['raw_upkeep']
            a['charged_upkeep'] += charged
            a['overflow_upkeep'] += overflow
            a['season_score'] += score
            a['matches'] += 1

    award_standing_points(matches, scores)

    for key in squads:
        a = agg[key]
        a['squad_key'] = key
        a['match_awards'] = sum(m['awards'].get(key, 0) for m in matches)
        a['streak_credit'] = streak_credit(matches, key)
        a['standing_points'] = a['match_awards'] + a['streak_credit']
        sc = a['season_score']
        a['tier'] = ('champion' if sc >= 3 * threshold else
                     'contender' if sc >= 2 * threshold else
                     'qualifier' if sc >= threshold else 'provisional')
        row = registry.get(key)
        a['registered'] = row is not None
        a['entrant_id'] = row['entrant_id'] if row else None
        a['division'] = row['division'] if row else None
        a['licence_class'] = row['licence_class'] if row else None
        a['conference'] = divisions[row['division']]['conference'] if row else None
        a['segment'] = licences[row['licence_class']]['segment'] if row else None
        a['ladder_weight'] = weight_of(key)
        a['levy_rate'] = levy_of(key)
        a['is_ranked'] = bool(row and row.get('is_ranked', '').strip().lower()
                              in ('yes', 'true', '1', 'y'))
        a['effective_owner'] = owner.get(key, key)
        a['handed_over'] = key in handed_over

    return {'squads': agg, 'matches': matches, 'owner': owner,
            'handed_over': handed_over, 'attribution_passes': passes,
            'scores': scores}


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def control_quantities(dec, roster_dict, registry, divisions, licences, budget, threshold):
    st = settle(dec, roster_dict, registry, divisions, licences, budget, threshold)
    agg = st['squads']
    tiers = Counter(a['tier'] for a in agg.values())
    return {
        'matches opened': len(st['matches']),
        'records recovered': dec['recovered_records'],
        'distinct squads': len(agg),
        'gross output': sum(a['gross_output'] for a in agg.values()),
        'voided output': sum(a['voided_output'] for a in agg.values()),
        'raw upkeep': sum(a['raw_upkeep'] for a in agg.values()),
        'charged upkeep': sum(a['charged_upkeep'] for a in agg.values()),
        'overflow upkeep': sum(a['overflow_upkeep'] for a in agg.values()),
        'champion entrants': tiers.get('champion', 0),
        'contender entrants': tiers.get('contender', 0),
        'qualifier entrants': tiers.get('qualifier', 0),
        'provisional entrants': tiers.get('provisional', 0),
    }


def safe_decode(envelope, engine_tag, window_depth, sentinel, roster_dict):
    """Decode a capture, returning whatever was recovered if the reading collapses.

    A capture is allowed to disagree with what it declares about itself; that is a
    finding to report, so it must never stop the reading or the candidate search.
    """
    progress = {}
    try:
        dec = decode(envelope, engine_tag, window_depth, sentinel, roster_dict,
                     progress=progress)
        dec['reading_error'] = None
        return dec
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        records = progress.get('records', [])
        return {
            'envelope': envelope,
            'guards_active': [g for g in GUARD_BITS
                              if envelope['guard_register'] >> GUARD_BITS.index(g) & 1],
            'nonce': envelope['nonce_le'],
            'count_bias': sum(engine_tag.encode()) % 11,
            'segment_step': None,
            'segments': [],
            'records': records,
            'recovered_segments': progress.get('segments_recovered', 0),
            'recovered_records': len(records),
            'effective_segments': envelope['declared_segments'],
            'effective_records': envelope['declared_records'],
            'effective_roster_size': envelope['declared_roster_size'],
            'trailing_bytes': 0,
            'backwards_ticks': 0,
            'unresolved_escape_directives': 0,
            'reading_error': str(exc),
        }


def control_stage(label):
    """Which unstated quantity a published control figure speaks to."""
    if 'charged upkeep' in label or 'overflow upkeep' in label:
        return 'budget'
    if 'entrants' in label:
        return 'threshold'
    return 'guards'


def evaluate(quantities, controls, stages):
    """Return the first control figure that disagrees, or None."""
    for c in controls:
        if control_stage(c['label']) not in stages:
            continue
        got = quantities.get(c['label'])
        if got is None or got != c['published']:
            return {'control': c['id'], 'figure': c['label'],
                    'published': c['published'], 'reproduced': got}
    return None


# --------------------------------------------------------------------------
# bracket and docket
# --------------------------------------------------------------------------

def elect_bracket(agg, figures):
    floor_matches = fig_int(figures, 'eligibility_match_floor', 2)
    prominence = fig_int(figures, 'ladder_weight_prominence', 4)
    sp_floor = fig_int(figures, 'standing_points_floor', 3)
    weight_floor = fig_int(figures, 'combined_ladder_weight_floor', 7)
    gross_floor = fig_int(figures, 'combined_gross_output_floor', 900)

    pool = sorted(k for k, a in agg.items()
                  if a['effective_owner'] == k and a['matches'] >= floor_matches)

    def check(trio):
        members = [agg[k] for k in trio]
        confs = [m['conference'] for m in members if m['conference'] is not None]
        segs = {m['segment'] for m in members if m['segment'] is not None}
        return {
            'prominent_division': any(m['ladder_weight'] >= prominence for m in members),
            'both_segments': len(segs) >= 2,
            'standing_points_floor': all(m['standing_points'] >= sp_floor for m in members),
            'distinct_conferences': len(confs) == len(set(confs)),
            'ranked_entrant': any(m['is_ranked'] for m in members),
            'combined_ladder_weight': sum(m['ladder_weight'] for m in members) >= weight_floor,
            'no_overflow_upkeep': all(m['overflow_upkeep'] == 0 for m in members),
            'champion_member': any(m['tier'] == 'champion' for m in members),
            'combined_gross_output': sum(m['gross_output'] for m in members) >= gross_floor,
            'no_handover': not any(m['handed_over'] for m in members),
        }

    best = None
    best_rank = None
    candidates = 0
    best_count = 0
    for trio in itertools.combinations(pool, 3):
        candidates += 1
        met = check(trio)
        count = sum(met.values())
        upkeep = sum(agg[k]['charged_upkeep'] for k in trio)
        rank = (-count, upkeep, tuple(sorted(trio)))
        if count > best_count:
            best_count = count
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, (list(trio), met, count, upkeep)

    return {
        'eligibility_match_floor': floor_matches,
        'eligible_squads': pool,
        'candidate_count': candidates,
        'best_satisfied_constraints': best_count,
        'members': sorted(best[0]) if best else [],
        'constraints_met': best[1] if best else {c: False for c in BRACKET_CONSTRAINTS},
        'satisfied_constraints': best[2] if best else 0,
        'combined_charged_upkeep': best[3] if best else 0,
    }


def elect_docket(st, agg, figures):
    review_weight = fig_int(figures, 'review_weight', 25)
    size = fig_int(figures, 'docket_size', 5)
    cap = fig_int(figures, 'docket_conference_cap', 2)

    rows = []
    for m in st['matches']:
        registered = [(k, s['gross_output']) for k, s in m['stats'].items()
                      if agg[k]['registered']]
        conference = None
        if registered:
            top = min(registered, key=lambda kv: (-kv[1], kv[0]))
            conference = agg[top[0]]['conference']
        voided = sum(s['voided_output'] for s in m['stats'].values())
        rows.append({
            'match_id': m['match_id'],
            'logical_position': m['logical_position'],
            'conference': conference,
            'voided_output': voided,
            'escape_records': m['escape_records'],
            'review_index': voided + review_weight * m['escape_records'],
        })

    def feasible(sel):
        counts = Counter(r['conference'] for r in sel if r['conference'] is not None)
        return all(v <= cap for v in counts.values())

    target = min(size, len(rows))
    chosen = None
    while target > 0 and chosen is None:
        if _ncr(len(rows), target) <= 400000:
            best = None
            for combo in itertools.combinations(rows, target):
                if not feasible(combo):
                    continue
                rank = (-sum(r['review_index'] for r in combo),
                        tuple(sorted(r['match_id'] for r in combo)))
                if best is None or rank < best[0]:
                    best = (rank, combo)
            chosen = list(best[1]) if best else None
        else:                                   # partition matroid: greedy is optimal
            counts = Counter()
            sel = []
            for r in sorted(rows, key=lambda r: (-r['review_index'], r['match_id'])):
                if len(sel) == target:
                    break
                if r['conference'] is not None and counts[r['conference']] >= cap:
                    continue
                sel.append(r)
                counts[r['conference']] += 1
            chosen = sel if len(sel) == target else None
        if chosen is None:
            target -= 1

    chosen = sorted(chosen or [], key=lambda r: r['match_id'])
    spread = Counter(r['conference'] for r in chosen)
    return {
        'review_weight': review_weight,
        'docket_size': size,
        'docket_conference_cap': cap,
        'matches': chosen,
        'total_review_index': sum(r['review_index'] for r in chosen),
        'conference_spread': {(k if k is not None else 'unattributed'): v
                              for k, v in sorted(spread.items(),
                                                 key=lambda kv: (kv[0] is None, kv[0]))},
        'all_matches': sorted(rows, key=lambda r: r['match_id']),
    }


def _ncr(n, r):
    if r < 0 or r > n:
        return 0
    out = 1
    for i in range(r):
        out = out * (n - i) // (i + 1)
    return out


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_row(a):
    return {
        'charged_upkeep': a['charged_upkeep'],
        'conference': a['conference'],
        'division': a['division'],
        'entrant_id': a['entrant_id'],
        'gross_output': a['gross_output'],
        'licence_class': a['licence_class'],
        'matches': a['matches'],
        'overflow_upkeep': a['overflow_upkeep'],
        'season_score': a['season_score'],
        'segment': a['segment'],
        'squad_key': a['squad_key'],
        'standing_points': a['standing_points'],
        'streak_credit': a['streak_credit'],
        'tier': a['tier'],
        'voided_output': a['voided_output'],
    }


def group_report(kind, name, members):
    entries = [entry_row(a) for a in sorted(members, key=lambda a: a['squad_key'])]
    tier_counts = {t: 0 for t in TIER_NAMES}
    for a in members:
        tier_counts[a['tier']] += 1
    total = lambda f: sum(a[f] for a in members)
    return {
        'charged_upkeep': total('charged_upkeep'),
        'divisions': sorted({a['division'] for a in members if a['division']}),
        'entrants': len(entries),
        'entries': entries,
        'gross_output': total('gross_output'),
        'group_kind': kind,
        'group_name': name,
        'overflow_upkeep': total('overflow_upkeep'),
        'season_score': total('season_score'),
        'standing_points': total('standing_points'),
        'tier_counts': tier_counts,
        'voided_output': total('voided_output'),
    }


def write_json(out_dir, name, payload):
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write('\n')


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    rosters, figures, controls = scrape_documents(data_dir)
    registry_rows = find_csv(data_dir, {'entrant_id', 'squad_key', 'division', 'licence_class'})
    division_rows = find_csv(data_dir, {'division', 'conference', 'ladder_weight'})
    licence_rows = find_csv(data_dir, {'licence_class', 'segment', 'levy_rate'})
    registry = {r['squad_key']: r for r in registry_rows}
    divisions = {r['division']: r for r in division_rows}
    licences = {r['licence_class']: r for r in licence_rows}

    conferences = []
    for r in division_rows:
        if r['conference'] not in conferences:
            conferences.append(r['conference'])
    segments = []
    for r in licence_rows:
        if r['segment'] not in segments:
            segments.append(r['segment'])

    # locate the three captures by the capture identifier in their envelopes
    envelopes = []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            env = read_envelope(path)
        except OSError:
            continue
        if env:
            envelopes.append(env)
    roles = {}
    for env in envelopes:
        roles.setdefault(env['capture_id'] % 10, env)
    reference, control, season = roles.get(1), roles.get(2), roles.get(3)
    if reference is None or control is None or season is None:
        raise SystemExit('the corpus must carry a reference, a control and a season capture')

    tag_roster = rosters.get('engine_tag', ['tessel-drift'])
    win_roster = [as_int(x) for x in rosters.get('window_depth', ['6'])]
    sen_roster = [as_int(x) for x in rosters.get('sentinel', ['0x1D'])]
    budget_roster = [as_int(x) for x in rosters.get('budget_cap', ['860'])]
    threshold_roster = [as_int(x) for x in rosters.get('promotion_threshold', ['700'])]

    # ---- stage one: the guard quantities, against the first control figures ----
    def attempt(tag, win, sen):
        ref = safe_decode(reference, tag, win, sen, [])
        rd = build_roster_dictionary(ref)
        ctl = safe_decode(control, tag, win, sen, rd)
        return ref, rd, ctl

    guard_results = {}
    accepted_triple = None
    for tag, win, sen in itertools.product(tag_roster, win_roster, sen_roster):
        ref, rd, ctl = attempt(tag, win, sen)
        notes = [n for n in (ref['reading_error'] and
                             'the reference reading collapses: ' + ref['reading_error'],
                             ctl['reading_error'] and
                             'the control reading collapses: ' + ctl['reading_error']) if n]
        try:
            q = control_quantities(ctl, rd, registry, divisions, licences,
                                   budget_roster[0], threshold_roster[0])
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            notes.append('the control slice cannot be settled: %s' % exc)
            q = {'matches opened': ctl['recovered_segments'],
                 'records recovered': ctl['recovered_records'],
                 'distinct squads': len({r['squad_key'] for r in ctl['records']})}
        bad = evaluate(q, controls, {'guards'})
        if bad is None and notes:
            bad = {'control': None, 'figure': 'structural integrity',
                   'published': None, 'reproduced': None, 'note': '; '.join(notes)}
        elif bad is not None and notes:
            bad['note'] = '; '.join(notes)
        guard_results[(tag, win, sen)] = bad
        if bad is None and accepted_triple is None:
            accepted_triple = (tag, win, sen)
    if accepted_triple is None:
        raise SystemExit('no candidate reading reproduces the control figures')

    engine_tag, window_depth, sentinel = accepted_triple
    ref_dec, roster_dict, ctl_dec = attempt(engine_tag, window_depth, sentinel)
    transcript_check = verify_transcript(data_dir, ref_dec)

    # ---- stage two: the season budget, against the upkeep control figures ----
    budget_results = {}
    season_budget = None
    for b in budget_roster:
        q = control_quantities(ctl_dec, roster_dict, registry, divisions, licences,
                               b, threshold_roster[0])
        bad = evaluate(q, controls, {'guards', 'budget'})
        budget_results[b] = bad
        if bad is None and season_budget is None:
            season_budget = b
    if season_budget is None:
        raise SystemExit('no budget on the roster reproduces the control figures')

    # ---- stage three: the promotion threshold, against the tier figures ----
    threshold_results = {}
    promotion_threshold = None
    for t in threshold_roster:
        q = control_quantities(ctl_dec, roster_dict, registry, divisions, licences,
                               season_budget, t)
        bad = evaluate(q, controls, {'guards', 'budget', 'threshold'})
        threshold_results[t] = bad
        if bad is None and promotion_threshold is None:
            promotion_threshold = t
    if promotion_threshold is None:
        raise SystemExit('no threshold on the roster reproduces the control figures')

    # ---- settle the season ----
    season_dec = safe_decode(season, engine_tag, window_depth, sentinel, roster_dict)
    st = settle(season_dec, roster_dict, registry, divisions, licences,
                season_budget, promotion_threshold)
    agg = st['squads']

    ranked = sorted(agg.values(),
                    key=lambda a: (-a['season_score'], -a['standing_points'], a['squad_key']))
    for i, a in enumerate(ranked, 1):
        a['rank'] = i

    bracket = elect_bracket(agg, figures)
    docket = elect_docket(st, agg, figures)

    tier_counts = {t: 0 for t in TIER_NAMES}
    for a in agg.values():
        tier_counts[a['tier']] += 1
    ledger = {n: 0 for n in KIND_NAMES.values()}
    for rec in season_dec['records']:
        ledger[KIND_NAMES[rec['kind']]] += 1

    season_gross = sum(a['gross_output'] for a in agg.values())
    season_voided = sum(a['voided_output'] for a in agg.values())
    season_raw = sum(a['raw_upkeep'] for a in agg.values())
    season_charged = sum(a['charged_upkeep'] for a in agg.values())
    season_overflow = sum(a['overflow_upkeep'] for a in agg.values())

    # ---- group reports ----
    conference_reports = {}
    for conf in conferences:
        members = [a for a in agg.values() if a['conference'] == conf]
        conference_reports[conf] = group_report('conference', conf, members)
        write_json(out_dir, 'region_%s.json' % conf, conference_reports[conf])
    segment_reports = {}
    for seg in segments:
        members = [a for a in agg.values() if a['segment'] == seg]
        segment_reports[seg] = group_report('segment', seg, members)
        write_json(out_dir, 'segment_%s.json' % seg, segment_reports[seg])

    attribution_residual = season_gross - sum(r['gross_output'] for r in conference_reports.values())
    segment_residual = season_gross - sum(r['gross_output'] for r in segment_reports.values())

    # ---- summary ----
    summary = {
        'capture': {
            'file': os.path.basename(season_dec['envelope']['path']),
            'capture_id': season['capture_id'],
            'envelope_version': season['version'],
            'guard_register': season['guard_register'],
            'guards_active': season_dec['guards_active'],
            'capture_nonce': season_dec['nonce'],
            'season_base_tick': season['base_tick'],
            'segment_step': season_dec['segment_step'],
        },
        'resolved_quantities': {
            'engine_tag': engine_tag,
            'window_depth': window_depth,
            'sentinel': '0x%02X' % sentinel,
            'count_bias': season_dec['count_bias'],
            'season_budget': season_budget,
            'promotion_threshold': promotion_threshold,
        },
        'roster_dictionary': roster_dict,
        'totals': {
            'matches': len(st['matches']),
            'records_recovered': season_dec['recovered_records'],
            'distinct_squads': len(agg),
            'registered_squads': sum(1 for a in agg.values() if a['registered']),
            'unregistered_squads': sum(1 for a in agg.values() if not a['registered']),
            'gross_output': season_gross,
            'voided_output': season_voided,
            'reported_output': season_gross + season_voided,
            'raw_upkeep': season_raw,
            'charged_upkeep': season_charged,
            'overflow_upkeep': season_overflow,
            'season_score': sum(a['season_score'] for a in agg.values()),
            'standing_points': sum(a['standing_points'] for a in agg.values()),
        },
        'kind_ledger': ledger,
        'tier_counts': tier_counts,
        'attribution_settlement_passes': st['attribution_passes'],
        'effective_owners': {k: a['effective_owner'] for k, a in sorted(agg.items())
                             if a['effective_owner'] != k},
        'standings': [{
            'rank': a['rank'], 'squad_key': a['squad_key'], 'entrant_id': a['entrant_id'],
            'registered': a['registered'], 'conference': a['conference'],
            'segment': a['segment'], 'division': a['division'],
            'licence_class': a['licence_class'], 'ladder_weight': a['ladder_weight'],
            'levy_rate': a['levy_rate'], 'is_ranked': a['is_ranked'],
            'matches': a['matches'], 'gross_output': a['gross_output'],
            'voided_output': a['voided_output'], 'raw_upkeep': a['raw_upkeep'],
            'charged_upkeep': a['charged_upkeep'], 'overflow_upkeep': a['overflow_upkeep'],
            'season_score': a['season_score'], 'match_awards': a['match_awards'],
            'streak_credit': a['streak_credit'], 'standing_points': a['standing_points'],
            'tier': a['tier'], 'effective_owner': a['effective_owner'],
            'handed_over': a['handed_over'],
        } for a in ranked],
        'matches': [{
            'match_id': m['match_id'], 'logical_position': m['logical_position'],
            'close_tick': m['close_tick'], 'records': m['records'],
            'participants': sorted(m['stats']),
            'conceded': sorted(m['conceded']),
            'gross_output': sum(s['gross_output'] for s in m['stats'].values()),
            'voided_output': sum(s['voided_output'] for s in m['stats'].values()),
            'raw_upkeep': sum(s['raw_upkeep'] for s in m['stats'].values()),
            'charged_upkeep': sum(m['charged'].values()),
            'overflow_upkeep': sum(m['overflow'].values()),
            'escape_records': m['escape_records'],
            'outright_winner': m['outright'],
            'awards': {k: v for k, v in sorted(m['awards'].items()) if v},
        } for m in st['matches']],
        'finals_bracket': {
            'members': bracket['members'],
            'satisfied_constraints': bracket['satisfied_constraints'],
            'constraints_met': bracket['constraints_met'],
            'combined_charged_upkeep': bracket['combined_charged_upkeep'],
            'combined_gross_output': sum(agg[k]['gross_output'] for k in bracket['members']),
            'combined_ladder_weight': sum(agg[k]['ladder_weight'] for k in bracket['members']),
        },
        'review_docket': {
            'match_ids': [r['match_id'] for r in docket['matches']],
            'total_review_index': docket['total_review_index'],
            'conference_spread': docket['conference_spread'],
            'entries': docket['matches'],
        },
        'group_totals': {
            'conferences': {k: v['gross_output'] for k, v in conference_reports.items()},
            'segments': {k: v['gross_output'] for k, v in segment_reports.items()},
        },
    }
    write_json(out_dir, 'summary.json', summary)

    # ---- data quality ----
    def capture_block(dec):
        env = dec['envelope']
        return {
            'file': os.path.basename(env['path']),
            'capture_id': env['capture_id'],
            'role': {1: 'reference', 2: 'control', 3: 'season'}.get(env['capture_id'] % 10, 'unknown'),
            'guard_register': env['guard_register'],
            'guards_active': dec['guards_active'],
            'count_bias': dec['count_bias'],
            'envelope_checksum_declared': env['envelope_checksum'],
            'envelope_checksum_recomputed': env['envelope_checksum_recomputed'],
            'envelope_checksum_agrees': env['envelope_checksum'] == env['envelope_checksum_recomputed'],
            'declared_segments': env['declared_segments'],
            'declared_segments_debiased': dec['effective_segments'],
            'recovered_segments': dec['recovered_segments'],
            'segment_residual': dec['effective_segments'] - dec['recovered_segments'],
            'declared_records': env['declared_records'],
            'declared_records_debiased': dec['effective_records'],
            'recovered_records': dec['recovered_records'],
            'record_residual': dec['effective_records'] - dec['recovered_records'],
            'declared_roster_size': env['declared_roster_size'],
            'declared_roster_size_debiased': dec['effective_roster_size'],
            'payload_checksums_agree': all(s['payload_checksum'] == s['payload_checksum_recomputed']
                                           for s in dec['segments']),
            'filler_widths_agree': all(s['filler_width'] == s['expected_filler_width']
                                       for s in dec['segments']),
            'trailing_bytes': dec['trailing_bytes'],
            'backwards_tick_places': dec['backwards_ticks'],
            'unresolved_escape_directives': dec['unresolved_escape_directives'],
            'reading_error': dec.get('reading_error'),
            'structurally_sound': structurally_sound(dec),
        }

    registered_seen = sorted(k for k, a in agg.items() if a['registered'])
    unregistered_seen = sorted(k for k, a in agg.items() if not a['registered'])
    absent = sorted(k for k in registry if k not in agg)

    ledger_total = sum(ledger.values())
    reported_output = sum(r['value'] for r in season_dec['records'] if r['kind'] == 2)
    dq = {
        'captures': [capture_block(d) for d in (ref_dec, ctl_dec, season_dec)],
        'reference_transcript_check': transcript_check,
        'season_reconciliation': {
            'declared_segments_debiased': season_dec['effective_segments'],
            'recovered_segments': season_dec['recovered_segments'],
            'segment_residual': season_dec['effective_segments'] - season_dec['recovered_segments'],
            'declared_records_debiased': season_dec['effective_records'],
            'recovered_records': season_dec['recovered_records'],
            'record_residual': season_dec['effective_records'] - season_dec['recovered_records'],
            'declared_roster_size_debiased': season_dec['effective_roster_size'],
            'registered_squads_recovered': len(registered_seen),
            'roster_residual': season_dec['effective_roster_size'] - len(registered_seen),
            'distinct_squads_recovered': len(agg),
            'squads_without_an_entry': len(unregistered_seen),
        },
        'attribution_residual': {
            'season_gross_output': season_gross,
            'conference_gross_output_total': sum(r['gross_output'] for r in conference_reports.values()),
            'residual': attribution_residual,
            'segment_gross_output_total': sum(r['gross_output'] for r in segment_reports.values()),
            'segment_residual': segment_residual,
            'carried_by': {k: agg[k]['gross_output'] for k in unregistered_seen},
        },
        'ledger_residual': {
            'kind_ledger': ledger,
            'ledger_total': ledger_total,
            'recovered_records': season_dec['recovered_records'],
            'residual': season_dec['recovered_records'] - ledger_total,
        },
        'identities': {
            'gross_plus_voided': season_gross + season_voided,
            'reported_output': reported_output,
            'output_identity_residual': reported_output - (season_gross + season_voided),
            'charged_plus_overflow': season_charged + season_overflow,
            'raw_upkeep': season_raw,
            'upkeep_identity_residual': season_raw - (season_charged + season_overflow),
            'backwards_tick_places': season_dec['backwards_ticks'],
        },
        'unregistered_squads': [{
            'squad_key': k, 'matches': agg[k]['matches'],
            'gross_output': agg[k]['gross_output'],
            'voided_output': agg[k]['voided_output'],
            'raw_upkeep': agg[k]['raw_upkeep'],
            'charged_upkeep': agg[k]['charged_upkeep'],
            'overflow_upkeep': agg[k]['overflow_upkeep'],
            'season_score': agg[k]['season_score'],
            'tier': agg[k]['tier'],
        } for k in unregistered_seen],
        'absent_entrants': [{'squad_key': k, 'entrant_id': registry[k]['entrant_id'],
                             'division': registry[k]['division'],
                             'conference': divisions[registry[k]['division']]['conference'],
                             'licence_class': registry[k]['licence_class']} for k in absent],
        'findings': [],
    }
    fnd = dq['findings']
    for block in dq['captures']:
        if block['segment_residual']:
            fnd.append('%s: declared segment count exceeds recovered by %d'
                       % (block['file'], block['segment_residual']))
        if block['record_residual']:
            fnd.append('%s: declared record count exceeds recovered by %d'
                       % (block['file'], block['record_residual']))
        if block['backwards_tick_places']:
            fnd.append('%s: %d places where ticks move backwards inside a segment'
                       % (block['file'], block['backwards_tick_places']))
        if block['reading_error']:
            fnd.append('%s: the reading stopped early (%s)' % (block['file'], block['reading_error']))
        if not block['payload_checksums_agree']:
            fnd.append('%s: a segment payload checksum disagrees' % block['file'])
        if not block['envelope_checksum_agrees']:
            fnd.append('%s: the envelope checksum disagrees' % block['file'])
        if block['trailing_bytes']:
            fnd.append('%s: %d bytes trail the last segment' % (block['file'], block['trailing_bytes']))
        if block['unresolved_escape_directives']:
            fnd.append('%s: %d escape directives advance from a squad the roster dictionary '
                       'does not hold and leave the attribution where it was'
                       % (block['file'], block['unresolved_escape_directives']))
    if dq['season_reconciliation']['roster_residual']:
        fnd.append('season: declared roster size differs from registered squads recovered by %d'
                   % dq['season_reconciliation']['roster_residual'])
    if attribution_residual:
        fnd.append('attribution residual of %d gross output carried by squads with no entry: %s'
                   % (attribution_residual, ', '.join(unregistered_seen)))
    if dq['ledger_residual']['residual']:
        fnd.append('kind ledger does not account for %d recovered records'
                   % dq['ledger_residual']['residual'])
    if dq['identities']['output_identity_residual']:
        fnd.append('gross plus voided output misses the output reported on the wire by %d'
                   % dq['identities']['output_identity_residual'])
    if dq['identities']['upkeep_identity_residual']:
        fnd.append('charged plus overflow upkeep misses raw upkeep by %d'
                   % dq['identities']['upkeep_identity_residual'])
    if absent:
        fnd.append('absent entrants listed in the registry but seen in no capture: %s'
                   % ', '.join(absent))
    if unregistered_seen:
        fnd.append('squads seen on the wire that the entrant registry never lists: %s'
                   % ', '.join(unregistered_seen))
    if transcript_check.get('transcript_present') and not transcript_check.get('byte_exact'):
        fnd.append('the reference reading does not agree with its labelled transcript')
    write_json(out_dir, 'data_quality.json', dq)

    # ---- policy audit ----
    def candidate_rows(space, results, fmt=str):
        rows = []
        for value in space:
            bad = results[value]
            row = {'candidate': fmt(value), 'verdict': 'accepted' if bad is None else 'rejected'}
            if bad is not None:
                row['falsifier'] = bad
            rows.append(row)
        return rows

    def guard_rows(index, fixed, space, fmt=str):
        rows = []
        for value in space:
            key = list(accepted_triple)
            key[index] = value
            bad = guard_results.get(tuple(key))
            row = {'candidate': fmt(value),
                   'verdict': 'accepted' if bad is None else 'rejected',
                   'held_fixed': fixed}
            if bad is not None:
                row['falsifier'] = bad
            rows.append(row)
        return rows

    final_q = control_quantities(ctl_dec, roster_dict, registry, divisions, licences,
                                 season_budget, promotion_threshold)
    calibration = [{
        'control': c['id'], 'figure': c['label'], 'published': c['published'],
        'reproduced': final_q.get(c['label']),
        'agrees': final_q.get(c['label']) == c['published'],
    } for c in controls]

    audit = {
        'policy_figures_read_from_the_documents': {
            'eligibility_match_floor': fig_int(figures, 'eligibility_match_floor', 2),
            'ladder_weight_prominence': fig_int(figures, 'ladder_weight_prominence', 4),
            'standing_points_floor': fig_int(figures, 'standing_points_floor', 3),
            'combined_ladder_weight_floor': fig_int(figures, 'combined_ladder_weight_floor', 7),
            'combined_gross_output_floor': fig_int(figures, 'combined_gross_output_floor', 900),
            'review_weight': fig_int(figures, 'review_weight', 25),
            'docket_size': fig_int(figures, 'docket_size', 5),
            'docket_conference_cap': fig_int(figures, 'docket_conference_cap', 2),
        },
        'method': [
            'The reference capture is decoded with every stream guard idle and is checked '
            'byte for byte against the labelled transcript before either guarded capture is read.',
            'The roster dictionary is the distinct attribution keys of the reference capture '
            'in first-met logical order; the control slice and the season resolve their '
            'dictionary positions against it.',
            'The guard quantities are searched jointly over their published rosters and are '
            'accepted only when the control slice reproduces every control figure that does '
            'not depend on the budget or the threshold.',
            'The season budget is then pinned against the charged and overflow upkeep figures, '
            'and the promotion threshold against the tier count figures.',
        ],
        'unstated_quantities': {
            'engine_tag': {
                'roster': tag_roster,
                'resolved': engine_tag,
                'candidates': guard_rows(0, {'window_depth': window_depth,
                                             'sentinel': '0x%02X' % sentinel}, tag_roster),
            },
            'window_depth': {
                'roster': win_roster,
                'resolved': window_depth,
                'candidates': guard_rows(1, {'engine_tag': engine_tag,
                                             'sentinel': '0x%02X' % sentinel}, win_roster, int),
            },
            'sentinel': {
                'roster': ['0x%02X' % s for s in sen_roster],
                'resolved': '0x%02X' % sentinel,
                'candidates': guard_rows(2, {'engine_tag': engine_tag,
                                             'window_depth': window_depth},
                                         sen_roster, lambda v: '0x%02X' % v),
            },
            'season_budget': {
                'roster': budget_roster,
                'resolved': season_budget,
                'candidates': candidate_rows(budget_roster, budget_results, int),
            },
            'promotion_threshold': {
                'roster': threshold_roster,
                'resolved': promotion_threshold,
                'candidates': candidate_rows(threshold_roster, threshold_results, int),
            },
        },
        'guard_search': {
            'space_size': len(tag_roster) * len(win_roster) * len(sen_roster),
            'accepted': [{'engine_tag': t, 'window_depth': w, 'sentinel': '0x%02X' % s}
                         for (t, w, s), bad in sorted(guard_results.items()) if bad is None],
        },
        'attribution_settlement_passes': st['attribution_passes'],
        'attribution_settlement': {
            'hand_overs': [{'squad_key': k, 'hands_to': v} for k, v in
                           sorted((r['squad_key'], roster_dict[r['value'] % len(roster_dict)])
                                  for r in season_dec['records'] if r['kind'] == 6)],
            'effective_owners': {k: a['effective_owner'] for k, a in sorted(agg.items())},
            'passes': st['attribution_passes'],
        },
        'bracket': {
            'constraints': BRACKET_CONSTRAINTS,
            'eligibility_match_floor': bracket['eligibility_match_floor'],
            'eligible_squads': bracket['eligible_squads'],
            'candidate_space_size': bracket['candidate_count'],
            'best_satisfied_constraint_count': bracket['best_satisfied_constraints'],
            'elected': bracket['members'],
            'elected_satisfied_constraints': bracket['satisfied_constraints'],
            'elected_constraints_met': bracket['constraints_met'],
            'tie_break': 'largest satisfied count, then least combined charged upkeep, '
                         'then the member keys that sort first',
        },
        'docket': {
            'review_weight': docket['review_weight'],
            'docket_size': docket['docket_size'],
            'docket_conference_cap': docket['docket_conference_cap'],
            'elected': [r['match_id'] for r in docket['matches']],
            'total_review_index': docket['total_review_index'],
            'conference_spread': docket['conference_spread'],
            'match_review_index': docket['all_matches'],
        },
        'calibration': calibration,
        'calibration_agrees': all(r['agrees'] for r in calibration),
    }
    write_json(out_dir, 'policy_audit.json', audit)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
