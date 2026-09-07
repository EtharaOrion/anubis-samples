#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR.  Standard library only.
"""

import collections
import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys

M32 = 1 << 32

GUARD_NAMES = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
               'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']

KIND_NAMES = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
              5: 'concede', 6: 'transfer', 8: 'close'}

TIER_ORDER = ['champion', 'contender', 'qualifier', 'provisional']

NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}


# --------------------------------------------------------------------------
# corpus discovery -- documents are found by their content, never by filename
# --------------------------------------------------------------------------

def _read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()


def discover(data_dir):
    corpus = {'captures': {}, 'transcripts': [], 'tables': {}, 'prose': ''}
    prose = []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, 'rb') as fh:
            head = fh.read(4)
        if head == b'RJNL':
            with open(path, 'rb') as fh:
                blob = fh.read()
            env = read_envelope(blob)
            role = {1: 'reference', 2: 'control', 3: 'season'}.get(
                env['capture_id'] % 10, 'capture_%d' % env['capture_id'])
            corpus['captures'][role] = {'path': path, 'name': name, 'data': blob}
            continue
        text = _read_text(path)
        stripped = text.lstrip()
        if stripped.startswith('{') and name.lower().endswith('.jsonl'):
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            corpus['transcripts'].append({'name': name, 'rows': rows})
            continue
        if name.lower().endswith('.csv'):
            rows = list(csv.DictReader(text.splitlines()))
            if not rows:
                continue
            cols = set(rows[0].keys())
            if {'division', 'conference', 'ladder_weight'} <= cols:
                corpus['tables']['topology'] = rows
            elif {'licence_class', 'segment', 'levy_rate'} <= cols:
                corpus['tables']['licences'] = rows
            elif {'squad_key', 'division', 'licence_class'} <= cols:
                corpus['tables']['registry'] = rows
            continue
        if name.lower().endswith('.md'):
            prose.append(text)
    corpus['prose'] = '\n'.join(prose)
    return corpus


def parse_roster(prose, label):
    """`Label roster: `a`, `b`, `c`` -> ['a','b','c']"""
    m = re.search(re.escape(label) + r'\s*:?\s*(.+)', prose)
    if not m:
        return []
    line = m.group(1)
    items = re.findall(r'`([^`]+)`', line)
    return [i.strip() for i in items]


def parse_scalar(prose, label, default=None):
    m = re.search(re.escape(label) + r'\s*:?\s*`?([-+]?(?:0x)?[0-9A-Fa-f]+)`?', prose)
    if not m:
        return default
    return _as_int(m.group(1))


def _as_int(tok):
    tok = tok.strip().strip('`')
    if tok.lower().startswith('0x'):
        return int(tok, 16)
    return int(tok)


def parse_controls(prose):
    """Published calibration figures: '- C1 matches opened: 5'."""
    figures = []
    for m in re.finditer(r'^\s*[-*]\s*(C\d+)\s+([^:]+):\s*([0-9]+)\s*$',
                         prose, re.M):
        figures.append({'id': m.group(1), 'name': m.group(2).strip(),
                        'published': int(m.group(3))})
    figures.sort(key=lambda f: int(f['id'][1:]))
    return figures


def figure_stage(name):
    n = name.lower()
    if 'charged' in n or 'overflow' in n:
        return 'budget'
    for t in TIER_ORDER:
        if t in n:
            return 'threshold'
    return 'guard'


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

def read_envelope(data):
    e = data[:36]
    if e[:4] != b'RJNL':
        raise ValueError('not a replay journal')
    reg = struct.unpack_from('<H', e, 6)[0]
    guards = {n: bool(reg >> i & 1) for i, n in enumerate(GUARD_NAMES)}
    nonce = struct.unpack_from('>I' if guards['endian'] else '<I', e, 8)[0]
    segc, recc, base_tick, capid, roster, cks = struct.unpack_from('<6I', e, 12)
    return {'version': e[4], 'guard_register': reg, 'guards': guards,
            'nonce': nonce, 'decl_segments': segc, 'decl_records': recc,
            'base_tick': base_tick, 'capture_id': capid,
            'decl_roster': roster, 'checksum': cks,
            'checksum_ok': sum(e[:32]) % M32 == cks}


def read_segments(data):
    off, segs = 36, []
    while off < len(data):
        if data[off:off + 4] != b'SEG0':
            raise ValueError('lost segment framing at %d' % off)
        slot, mid, rc, salt, pw, pc = struct.unpack_from('<6I', data, off + 4)
        payload = data[off + 28:off + 28 + pw]
        if len(payload) != pw:
            raise ValueError('truncated segment payload')
        segs.append({'slot': slot, 'match_id': mid, 'decl_records': rc,
                     'salt': salt, 'payload_width': pw, 'payload_checksum': pc,
                     'payload': payload,
                     'checksum_ok': sum(payload) % M32 == pc})
        off += 28 + pw
    return segs


def perm_step(nonce, n):
    step = ((nonce >> 8) % n) + 2
    while True:
        a, b = step, n
        while b:
            a, b = b, a % b
        if a == 1:
            return step
        step += 1


def logical_order(env, nseg):
    """order[logical_position] = storage index."""
    if not env['guards']['segperm'] or nseg == 0:
        return list(range(nseg))
    step = perm_step(env['nonce'], nseg)
    order = [None] * nseg
    for i in range(nseg):
        order[(env['nonce'] + i * step) % nseg] = i
    if any(x is None for x in order):
        raise ValueError('segment permutation is not a permutation')
    return order


def decode_capture(data, engine_tag, window, sentinel, roster_dict):
    """Recover the record stream of a capture in logical order."""
    env = read_envelope(data)
    G = env['guards']
    segs = read_segments(data)
    bias = (sum(engine_tag.encode()) % 11) if G['countbias'] else 0
    order = logical_order(env, len(segs))

    recs = []
    gidx = 0
    prev_tick = None
    running_val = 0          # sum of value fields of logically earlier segments
    window_list = []
    backward_ticks = 0

    for lidx, sidx in enumerate(order):
        seg = segs[sidx]
        count = seg['decl_records'] - bias
        if count < 0:
            raise ValueError('negative segment record count')
        salt = (seg['salt'] ^ running_val) % M32 if G['driftkey'] else seg['salt']
        payload = seg['payload']
        pos = 0
        seg_val_sum = 0
        prev_seg_tick = 0
        prev_d1 = 0
        first_in_seg = True

        for r in range(count):
            if pos + 2 > len(payload):
                raise ValueError('frame overruns payload')
            visible, shadow = payload[pos], payload[pos + 1]
            width = (shadow ^ (salt & 0xFF)) if G['shadow'] else visible
            if width <= 0 or pos + 2 + width > len(payload):
                raise ValueError('impossible frame width')
            blob = payload[pos + 2:pos + 2 + width]
            pos += 2 + width

            if G['keystream']:
                key = hashlib.sha256(
                    ('%s:%d:%d:%d' % (engine_tag, seg['match_id'], salt, r)
                     ).encode()).digest()
                if width > len(key):
                    raise ValueError('keystream shorter than blob')
                blob = bytes(a ^ b for a, b in zip(blob, key[:width]))
            if G['rotate']:
                turn = (salt + gidx) % width
                if turn:
                    blob = blob[-turn:] + blob[:-turn]

            # Directive pairs sit ahead of the body.  They are prepended when
            # the record is sealed, so the pair read first was applied last.
            directives = []
            if G['escape']:
                while len(directives) < 2 and len(blob) >= 2 and blob[0] == sentinel:
                    directives.append(blob[1])
                    blob = blob[2:]

            if len(blob) < 3:
                raise ValueError('record body too short')
            kind, mode = blob[0], blob[1]
            if kind not in KIND_NAMES:
                raise ValueError('unknown event kind %d' % kind)
            if mode == 0:
                raw = blob[2:5]
                head = 5
                try:
                    wire_key = raw.decode('ascii')
                except UnicodeDecodeError:
                    raise ValueError('attribution key is not ASCII')
                if not wire_key.isalnum():
                    raise ValueError('attribution key is not alphanumeric')
            elif mode == 1:
                head = 3
                slot = blob[2]
                if slot >= len(window_list):
                    raise ValueError('back reference past the window')
                wire_key = window_list[slot]
            elif mode == 2:
                head = 3
                if not roster_dict:
                    raise ValueError('no roster dictionary for alias')
                wire_key = roster_dict[blob[2] % len(roster_dict)]
            else:
                raise ValueError('unknown attribution mode %d' % mode)
            if len(blob) != head + 10:
                raise ValueError('record body length disagrees with mode')
            unit, tick_field, value_field = struct.unpack_from('<HII', blob, head)

            # the rolling window is refreshed with the key as written on the
            # wire, before any escape directive has been weighed
            if G['backref']:
                if wire_key in window_list:
                    window_list.remove(wire_key)
                window_list.append(wire_key)
                while len(window_list) > window:
                    window_list.pop(0)

            squad_key = wire_key
            for byte in reversed(directives):
                op, operand = byte >> 4, byte & 0xF
                size = len(roster_dict)
                if not size:
                    raise ValueError('no roster dictionary for escape')
                if op == 1:
                    squad_key = roster_dict[operand % size]
                elif op == 2:
                    if squad_key not in roster_dict:
                        raise ValueError('escape advance from unknown key')
                    squad_key = roster_dict[
                        (roster_dict.index(squad_key) + operand) % size]
                else:
                    raise ValueError('unknown escape operation %d' % op)

            if G['delta2']:
                d1 = (prev_d1 + tick_field) % M32
                tick = (prev_seg_tick + d1) % M32
                prev_d1, prev_seg_tick = d1, tick
            else:
                tick = tick_field
                if not first_in_seg and tick < prev_seg_tick:
                    backward_ticks += 1
                prev_seg_tick = tick
            if G['delta2'] and not first_in_seg and tick < recs[-1]['tick'] \
                    and recs[-1]['logical_segment'] == lidx:
                backward_ticks += 1
            first_in_seg = False

            if G['chain']:
                mask = env['nonce'] if prev_tick is None else prev_tick
                value = value_field ^ mask
            else:
                value = value_field
            prev_tick = tick
            seg_val_sum = (seg_val_sum + value) % M32

            recs.append({'kind': kind, 'mode': mode, 'wire_key': wire_key,
                         'squad_key': squad_key, 'escapes': len(directives),
                         'unit': unit, 'tick': tick, 'value': value,
                         'match_id': seg['match_id'], 'logical_segment': lidx,
                         'record_ordinal': gidx})
            gidx += 1

        filler = 3 + (seg['salt'] % 5)
        if len(payload) - pos != filler:
            raise ValueError('filler width disagrees (%d, expected %d)'
                             % (len(payload) - pos, filler))
        running_val = (running_val + seg_val_sum) % M32

    return {'envelope': env, 'segments': segs, 'order': order,
            'records': recs, 'bias': bias, 'backward_ticks': backward_ticks}


def build_dictionary(decoded):
    """Distinct attribution keys as written on the wire, in first-met order."""
    seen = []
    for rec in decoded['records']:
        if rec['wire_key'] not in seen:
            seen.append(rec['wire_key'])
    return seen


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

class League(object):
    def __init__(self, tables):
        self.divisions = {r['division']: r for r in tables.get('topology', [])}
        self.licences = {r['licence_class']: r for r in tables.get('licences', [])}
        self.registry = {r['squad_key']: r for r in tables.get('registry', [])}
        self.conferences = []
        for r in tables.get('topology', []):
            if r['conference'] not in self.conferences:
                self.conferences.append(r['conference'])
        self.segments = []
        for r in tables.get('licences', []):
            if r['segment'] not in self.segments:
                self.segments.append(r['segment'])

    def registered(self, key):
        return key in self.registry

    def division(self, key):
        return self.registry[key]['division'] if key in self.registry else None

    def conference(self, key):
        d = self.division(key)
        return self.divisions[d]['conference'] if d in self.divisions else None

    def ladder_weight(self, key):
        d = self.division(key)
        if d in self.divisions:
            return int(self.divisions[d]['ladder_weight'])
        return 1

    def licence_class(self, key):
        return self.registry[key]['licence_class'] if key in self.registry else None

    def segment(self, key):
        lc = self.licence_class(key)
        return self.licences[lc]['segment'] if lc in self.licences else None

    def levy_rate(self, key):
        lc = self.licence_class(key)
        if lc in self.licences:
            return int(self.licences[lc]['levy_rate'])
        return 1

    def entrant_id(self, key):
        return self.registry[key].get('entrant_id') if key in self.registry else None

    def is_ranked(self, key):
        if key not in self.registry:
            return False
        return str(self.registry[key].get('is_ranked', '')).strip().lower() in (
            'yes', 'true', '1', 'y')


def match_slices(records):
    """Per-match, per-squad gross / voided / raw upkeep, in logical order."""
    by_match = collections.OrderedDict()
    for rec in records:
        by_match.setdefault((rec['logical_segment'], rec['match_id']), []).append(rec)

    out = []
    for (lidx, mid), rs in by_match.items():
        elim, conceded, deploys, disbands = {}, {}, {}, {}
        close = None
        for r in rs:
            k = r['squad_key']
            if r['kind'] == 3:
                elim.setdefault((k, r['unit']), r['tick'])
            elif r['kind'] == 5:
                conceded.setdefault(k, r['tick'])
            elif r['kind'] == 1:
                deploys.setdefault((k, r['unit']), (r['tick'], r['value']))
            elif r['kind'] == 4:
                disbands.setdefault((k, r['unit']), r['tick'])
            elif r['kind'] == 8 and close is None:
                close = r['tick']
        if close is None:
            close = max(r['tick'] for r in rs) if rs else 0

        stats = collections.OrderedDict()

        def slot(k):
            if k not in stats:
                stats[k] = {'gross': 0, 'voided': 0, 'raw': 0}
            return stats[k]

        for r in rs:
            slot(r['squad_key'])
        for r in rs:
            if r['kind'] != 2:
                continue
            k = r['squad_key']
            e = elim.get((k, r['unit']))
            c = conceded.get(k)
            if (e is not None and r['tick'] >= e) or (c is not None and r['tick'] >= c):
                slot(k)['voided'] += r['value']
            else:
                slot(k)['gross'] += r['value']
        for (k, _u), (t, rate) in deploys.items():
            end = disbands.get((k, _u), close)
            slot(k)['raw'] += max(0, end - t) * rate

        out.append({'logical': lidx, 'match_id': mid, 'stats': stats,
                    'conceded': dict(conceded), 'close_tick': close,
                    'records': rs,
                    'escape_records': sum(1 for r in rs if r['escapes'])})
    return out


def settle(records, league, budget, threshold):
    slices = match_slices(records)
    remaining = {}
    agg = collections.OrderedDict()

    def row(k):
        if k not in agg:
            agg[k] = {'squad_key': k, 'gross_output': 0, 'voided_output': 0,
                      'raw_upkeep': 0, 'charged_upkeep': 0, 'overflow_upkeep': 0,
                      'matches': 0, 'match_awards': 0, 'streak_credit': 0,
                      'season_score': 0}
        return agg[k]

    win_sequence = collections.defaultdict(list)

    for sl in slices:
        scores = {}
        for k, v in sl['stats'].items():
            if k not in remaining:
                remaining[k] = budget
            charged = min(remaining[k], v['raw'])
            overflow = v['raw'] - charged
            remaining[k] -= charged
            score = v['gross'] * league.ladder_weight(k) - charged * league.levy_rate(k)
            scores[k] = score
            r = row(k)
            r['gross_output'] += v['gross']
            r['voided_output'] += v['voided']
            r['raw_upkeep'] += v['raw']
            r['charged_upkeep'] += charged
            r['overflow_upkeep'] += overflow
            r['matches'] += 1
            r['season_score'] += score
            v['charged'] = charged
            v['overflow'] = overflow
            v['score'] = score

        contenders = {k: s for k, s in scores.items() if k not in sl['conceded']}
        winners = []
        if contenders:
            top = max(contenders.values())
            winners = [k for k, s in contenders.items() if s == top]
            award = 3 if len(winners) == 1 else 1
            for k in winners:
                row(k)['match_awards'] += award
        outright = winners[0] if len(winners) == 1 else None
        for k in scores:
            win_sequence[k].append(k == outright)
        sl['scores'] = scores
        sl['winners'] = winners

    for k, seq in win_sequence.items():
        run = 0
        credit = 0
        for won in seq:
            if won:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        row(k)['streak_credit'] = credit

    for k, r in agg.items():
        r['standing_points'] = r['match_awards'] + r['streak_credit']
        s = r['season_score']
        if s >= 3 * threshold:
            r['tier'] = 'champion'
        elif s >= 2 * threshold:
            r['tier'] = 'contender'
        elif s >= threshold:
            r['tier'] = 'qualifier'
        else:
            r['tier'] = 'provisional'
    return slices, agg


def settle_ownership(records):
    """Follow entry hand overs to a fixpoint; report the sweeps taken."""
    hand = {}
    for rec in records:          # logical order; the last hand over stands
        if rec['kind'] == 6:
            hand[rec['squad_key']] = rec['value']
    return hand


def resolve_owners(records, dictionary):
    handed = {}
    for rec in records:
        if rec['kind'] == 6 and dictionary:
            handed[rec['squad_key']] = dictionary[rec['value'] % len(dictionary)]

    squads = set(r['squad_key'] for r in records)
    squads |= set(handed) | set(handed.values())
    squads = sorted(squads)

    step = {k: handed.get(k, k) for k in squads}

    # a closed set of hand overs settles on its first-sorting member
    colour = {}
    for start in squads:
        if start in colour:
            continue
        path, node = [], start
        while node not in colour and node not in path:
            path.append(node)
            node = step[node]
        if node in path:                       # found a cycle
            cycle = path[path.index(node):]
            anchor = min(cycle)
            for c in cycle:
                step[c] = anchor
            step[anchor] = anchor
        for n in path:
            colour[n] = True

    owner = {k: k for k in squads}
    passes = 0
    while True:
        passes += 1
        nxt = {k: step[owner[k]] for k in squads}
        if nxt == owner:
            break
        owner = nxt
    return handed, owner, passes


# --------------------------------------------------------------------------
# bracket and docket
# --------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    'at least one member sits in a division whose ladder weight reaches the prominence figure',
    'both entrant segments appear among the members',
    'every member holds at least the standing points floor',
    'no two members share a conference',
    'at least one member is a ranked entrant',
    'the ladder weights of the members sum to at least the combined ladder weight floor',
    'no member carries any overflow upkeep',
    'at least one member holds the champion tier',
    'the gross output of the members sums to at least the combined gross output floor',
    'no member has handed its entry to another squad',
]


def bracket_checks(members, agg, league, handed, policy):
    m = list(members)
    rows = [agg[k] for k in m]
    confs = [league.conference(k) for k in m]
    named = [c for c in confs if c]
    segs = set(league.segment(k) for k in m if league.segment(k))
    return [
        any(league.ladder_weight(k) >= policy['prominence'] for k in m),
        set(league.segments) <= segs if league.segments else False,
        all(r['standing_points'] >= policy['standing_floor'] for r in rows),
        len(named) == len(set(named)),
        any(league.is_ranked(k) for k in m),
        sum(league.ladder_weight(k) for k in m) >= policy['weight_floor'],
        all(r['overflow_upkeep'] == 0 for r in rows),
        any(r['tier'] == 'champion' for r in rows),
        sum(r['gross_output'] for r in rows) >= policy['gross_floor'],
        all(k not in handed for k in m),
    ]


def elect_bracket(agg, league, owner, handed, policy):
    pool = sorted(k for k, r in agg.items()
                  if owner.get(k, k) == k and r['matches'] >= policy['match_floor'])
    size = policy['bracket_size']
    best = None
    space = 0
    best_count = 0
    for combo in itertools.combinations(pool, size):
        space += 1
        checks = bracket_checks(combo, agg, league, handed, policy)
        met = sum(1 for c in checks if c)
        upkeep = sum(agg[k]['charged_upkeep'] for k in combo)
        cand = (-met, upkeep, list(combo))
        if best is None or cand < best[0]:
            best = (cand, combo, checks, met, upkeep)
        best_count = max(best_count, met)
    return {'pool': pool, 'candidate_space': space,
            'best_satisfied': best_count,
            'members': list(best[1]) if best else [],
            'satisfied': best[3] if best else 0,
            'checks': best[2] if best else [],
            'charged_upkeep': best[4] if best else 0}


def match_conference(sl, league):
    best = None
    for k, v in sl['stats'].items():
        if not league.registered(k):
            continue
        cand = (-v['gross'], k)
        if best is None or cand < best[0]:
            best = (cand, k)
    return league.conference(best[1]) if best else None


def elect_docket(slices, league, policy):
    entries = []
    for sl in slices:
        voided = sum(v['voided'] for v in sl['stats'].values())
        index = voided + policy['review_weight'] * sl['escape_records']
        entries.append({'match_id': sl['match_id'], 'logical': sl['logical'],
                        'conference': match_conference(sl, league),
                        'voided_output': voided,
                        'escape_records': sl['escape_records'],
                        'review_index': index})

    size = min(policy['docket_size'], len(entries))
    cap = policy['docket_cap']

    # only the top `cap` of each conference can ever be wanted
    groups = collections.defaultdict(list)
    for e in entries:
        groups[e['conference']].append(e)
    pool = []
    for conf, items in groups.items():
        items.sort(key=lambda e: (-e['review_index'], e['match_id']))
        pool.extend(items[:(size if conf is None else cap)])

    best = None
    for combo in itertools.combinations(sorted(pool, key=lambda e: e['match_id']), size):
        counts = collections.Counter(e['conference'] for e in combo
                                     if e['conference'] is not None)
        if counts and max(counts.values()) > cap:
            continue
        total = sum(e['review_index'] for e in combo)
        ids = sorted(e['match_id'] for e in combo)
        cand = (-total, ids)
        if best is None or cand < best[0]:
            best = (cand, combo, total)
    if best is None:
        return {'matches': [], 'total_review_index': 0, 'conference_spread': {},
                'all_matches': entries}
    chosen = sorted(best[1], key=lambda e: e['match_id'])
    spread = collections.Counter(e['conference'] if e['conference'] else 'unassigned'
                                 for e in chosen)
    return {'matches': chosen, 'total_review_index': best[2],
            'conference_spread': dict(sorted(spread.items())),
            'all_matches': entries}


# --------------------------------------------------------------------------
# calibration search
# --------------------------------------------------------------------------

def control_quantities(decoded, league, budget, threshold):
    recs = decoded['records']
    slices, agg = settle(recs, league, budget, threshold)
    tiers = collections.Counter(r['tier'] for r in agg.values())
    q = {
        'matches opened': len(decoded['segments']),
        'segments recovered': len(decoded['segments']),
        'records recovered': len(recs),
        'distinct squads': len(set(r['squad_key'] for r in recs)),
        'gross output': sum(r['gross_output'] for r in agg.values()),
        'voided output': sum(r['voided_output'] for r in agg.values()),
        'raw upkeep': sum(r['raw_upkeep'] for r in agg.values()),
        'charged upkeep': sum(r['charged_upkeep'] for r in agg.values()),
        'overflow upkeep': sum(r['overflow_upkeep'] for r in agg.values()),
        'standing points': sum(r['standing_points'] for r in agg.values()),
        'season score': sum(r['season_score'] for r in agg.values()),
    }
    for t in TIER_ORDER:
        q[t + ' entrants'] = tiers.get(t, 0)
        q[t + ' squads'] = tiers.get(t, 0)
    return q


def quantity_vocabulary():
    names = ['matches opened', 'segments recovered', 'records recovered',
             'distinct squads', 'gross output', 'voided output', 'raw upkeep',
             'charged upkeep', 'overflow upkeep', 'standing points', 'season score']
    for t in TIER_ORDER:
        names += [t + ' entrants', t + ' squads']
    return set(names)


VOCABULARY = quantity_vocabulary()


def known_figure(name):
    return name.strip().lower() in VOCABULARY


def lookup(quantities, name):
    n = name.strip().lower()
    if quantities is None:
        return None
    for k, v in quantities.items():
        if k.lower() == n:
            return v
    return None


def compare_stage(figures, stage, quantities):
    """Return (ok, falsifier_text).  A figure the corpus never explains is
    skipped rather than being allowed to falsify every candidate."""
    for f in figures:
        if figure_stage(f['name']) != stage or not known_figure(f['name']):
            continue
        got = lookup(quantities, f['name'])
        if got != f['published']:
            return False, '%s %s: published %d, reproduced %s' % (
                f['id'], f['name'], f['published'],
                'nothing (the capture would not decode under this reading)'
                if got is None else got)
    return True, None


def stage_matched(figures, stage, quantities):
    n = 0
    for f in figures:
        if figure_stage(f['name']) != stage or not known_figure(f['name']):
            continue
        if lookup(quantities, f['name']) == f['published']:
            n += 1
        else:
            break
    return n


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_row(key, r, league):
    return {
        'charged_upkeep': r['charged_upkeep'],
        'conference': league.conference(key),
        'division': league.division(key),
        'entrant_id': league.entrant_id(key),
        'gross_output': r['gross_output'],
        'licence_class': league.licence_class(key),
        'matches': r['matches'],
        'overflow_upkeep': r['overflow_upkeep'],
        'season_score': r['season_score'],
        'segment': league.segment(key),
        'squad_key': key,
        'standing_points': r['standing_points'],
        'streak_credit': r['streak_credit'],
        'tier': r['tier'],
        'voided_output': r['voided_output'],
    }


def group_report(kind, name, keys, agg, league):
    entries = [entry_row(k, agg[k], league) for k in sorted(keys)]
    rep = {
        'charged_upkeep': sum(e['charged_upkeep'] for e in entries),
        'entrants': len(entries),
        'entries': entries,
        'gross_output': sum(e['gross_output'] for e in entries),
        'group_kind': kind,
        'group_name': name,
        'overflow_upkeep': sum(e['overflow_upkeep'] for e in entries),
        'season_score': sum(e['season_score'] for e in entries),
        'standing_points': sum(e['standing_points'] for e in entries),
        'tier_counts': {t: sum(1 for e in entries if e['tier'] == t)
                        for t in TIER_ORDER},
        'voided_output': sum(e['voided_output'] for e in entries),
    }
    if kind == 'conference':
        rep['divisions'] = sorted({e['division'] for e in entries if e['division']})
    else:
        rep['licence_classes'] = sorted({e['licence_class'] for e in entries
                                         if e['licence_class']})
    return rep


def write_json(out_dir, name, obj):
    path = os.path.join(out_dir, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')
    return path


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    corpus = discover(data_dir)
    prose = corpus['prose']
    league = League(corpus['tables'])

    tag_roster = parse_roster(prose, 'Engine tag roster')
    window_roster = [_as_int(x) for x in parse_roster(prose, 'Window depth roster')]
    sentinel_roster = [_as_int(x) for x in parse_roster(prose, 'Sentinel roster')]
    budget_roster = [_as_int(x) for x in parse_roster(prose, 'Budget cap roster')]
    threshold_roster = [_as_int(x) for x in
                        parse_roster(prose, 'Promotion threshold roster')]
    figures = parse_controls(prose)

    bm = re.search(r'finals bracket holds exactly (\w+) squads', prose)
    bracket_size = NUMBER_WORDS.get(bm.group(1).lower(), 3) if bm else 3
    policy = {
        'bracket_size': bracket_size,
        'match_floor': parse_scalar(prose, 'Eligibility match floor', 2),
        'prominence': parse_scalar(prose, 'Ladder weight prominence', 4),
        'standing_floor': parse_scalar(prose, 'Standing points floor', 3),
        'weight_floor': parse_scalar(prose, 'Combined ladder weight floor', 7),
        'gross_floor': parse_scalar(prose, 'Combined gross output floor', 900),
        'review_weight': parse_scalar(prose, 'Review weight', 25),
        'docket_size': parse_scalar(prose, 'Docket size', 5),
        'docket_cap': parse_scalar(prose, 'Docket conference cap', 2),
    }

    ref = corpus['captures'].get('reference')
    ctl = corpus['captures'].get('control')
    season = corpus['captures'].get('season')
    if season is None:
        raise SystemExit('no sealed season capture in %s' % data_dir)

    # ---- stage 1: engine tag, window depth, sentinel -----------------------
    combos = []
    for tag in tag_roster:
        for win in window_roster:
            for sen in sentinel_roster:
                combos.append((tag, win, sen))

    attempts = []
    survivors = []
    ref_cache = {}
    transcript = corpus['transcripts'][0]['rows'] if corpus['transcripts'] else None
    transcript_report = {'available': transcript is not None}

    for tag, win, sen in combos:
        rec = {'engine_tag': tag, 'window_depth': win, 'sentinel': sen}
        try:
            if tag not in ref_cache:
                dref = decode_capture(ref['data'], tag, win, sen, []) if ref else None
                ref_cache[tag] = (dref, build_dictionary(dref) if dref else [])
            dref, dictionary = ref_cache[tag]
            dctl = decode_capture(ctl['data'], tag, win, sen, dictionary)
            q = control_quantities(dctl, league, budget_roster[0] if budget_roster else 0,
                                   threshold_roster[0] if threshold_roster else 1)
        except Exception as exc:              # noqa: BLE001 - candidate simply fails
            # keep whatever survives the failure so the falsifier names the
            # first figure this reading genuinely cannot reproduce
            try:
                segs = read_segments(ctl['data'])
                q = {'matches opened': len(segs), 'segments recovered': len(segs)}
            except Exception:
                q = None
            rec['error'] = str(exc)
        ok, falsifier = compare_stage(figures, 'guard', q)
        rec['matched'] = stage_matched(figures, 'guard', q)
        rec['verdict'] = 'accepted' if ok else 'rejected'
        rec['falsifier'] = falsifier
        attempts.append(rec)
        if ok:
            survivors.append((tag, win, sen))

    if not survivors:
        raise SystemExit('no reading of the guards reproduces the control figures')
    engine_tag, window_depth, sentinel = survivors[0]

    dref, dictionary = ref_cache[engine_tag]
    if transcript is not None and dref is not None:
        keys = sorted(transcript[0].keys())
        mine = [{k: r[k] for k in keys} for r in dref['records']]
        theirs = [{k: r[k] for k in keys} for r in transcript]
        transcript_report.update({
            'labelled_records': len(theirs),
            'recovered_records': len(mine),
            'byte_exact_agreement': mine == theirs,
            'first_disagreement': next(
                (i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b), None),
        })

    dctl = decode_capture(ctl['data'], engine_tag, window_depth, sentinel, dictionary)

    # ---- stage 2: season budget -------------------------------------------
    budget_attempts = []
    budget = None
    for cand in budget_roster:
        q = control_quantities(dctl, league, cand,
                               threshold_roster[0] if threshold_roster else 1)
        ok, falsifier = compare_stage(figures, 'budget', q)
        budget_attempts.append({'value': cand,
                                'verdict': 'accepted' if ok else 'rejected',
                                'falsifier': falsifier})
        if ok and budget is None:
            budget = cand
    if budget is None:
        raise SystemExit('no budget on the roster reproduces the control figures')

    # ---- stage 3: promotion threshold -------------------------------------
    threshold_attempts = []
    threshold = None
    for cand in threshold_roster:
        q = control_quantities(dctl, league, budget, cand)
        ok, falsifier = compare_stage(figures, 'threshold', q)
        threshold_attempts.append({'value': cand,
                                   'verdict': 'accepted' if ok else 'rejected',
                                   'falsifier': falsifier})
        if ok and threshold is None:
            threshold = cand
    if threshold is None:
        raise SystemExit('no threshold on the roster reproduces the control figures')

    control_final = control_quantities(dctl, league, budget, threshold)
    calibration = []
    for f in figures:
        got = lookup(control_final, f['name'])
        row = {'id': f['id'], 'figure': f['name'], 'stage': figure_stage(f['name']),
               'published': f['published'], 'reproduced': got,
               'agrees': got == f['published']}
        if not known_figure(f['name']):
            row['note'] = 'figure name not explained by the corpus; not compared'
        calibration.append(row)

    # ---- settle the season -------------------------------------------------
    dsea = decode_capture(season['data'], engine_tag, window_depth, sentinel,
                          dictionary)
    records = dsea['records']
    slices, agg = settle(records, league, budget, threshold)
    handed, owner, passes = resolve_owners(records, dictionary)

    standings = sorted(agg.values(),
                       key=lambda r: (-r['standing_points'], -r['season_score'],
                                      r['squad_key']))
    for i, r in enumerate(standings, 1):
        r['rank'] = i
        r['registered'] = league.registered(r['squad_key'])
        r['conference'] = league.conference(r['squad_key'])
        r['segment'] = league.segment(r['squad_key'])
        r['division'] = league.division(r['squad_key'])
        r['licence_class'] = league.licence_class(r['squad_key'])
        r['entrant_id'] = league.entrant_id(r['squad_key'])
        r['ladder_weight'] = league.ladder_weight(r['squad_key'])
        r['levy_rate'] = league.levy_rate(r['squad_key'])
        r['effective_owner'] = owner.get(r['squad_key'], r['squad_key'])
        r['handed_entry_to'] = handed.get(r['squad_key'])

    bracket = elect_bracket(agg, league, owner, handed, policy)
    docket = elect_docket(slices, league, policy)

    seen = [r['squad_key'] for r in standings]
    unregistered = sorted(k for k in seen if not league.registered(k))
    absent = sorted(k for k in league.registry if k not in agg)
    ledger = collections.Counter(KIND_NAMES[r['kind']] for r in records)

    season_gross = sum(r['gross_output'] for r in agg.values())
    season_voided = sum(r['voided_output'] for r in agg.values())
    season_raw = sum(r['raw_upkeep'] for r in agg.values())
    season_charged = sum(r['charged_upkeep'] for r in agg.values())
    season_overflow = sum(r['overflow_upkeep'] for r in agg.values())

    conf_reports = {}
    for conf in league.conferences:
        keys = [k for k in agg if league.conference(k) == conf]
        conf_reports[conf] = group_report('conference', conf, keys, agg, league)
    seg_reports = {}
    for seg in league.segments:
        keys = [k for k in agg if league.segment(k) == seg]
        seg_reports[seg] = group_report('segment', seg, keys, agg, league)

    conf_gross = sum(r['gross_output'] for r in conf_reports.values())
    seg_gross = sum(r['gross_output'] for r in seg_reports.values())
    attribution_residual = season_gross - conf_gross
    segment_residual = season_gross - seg_gross

    env = dsea['envelope']
    recovered_registered = sum(1 for k in agg if league.registered(k))
    roster_residual = len(agg) - (env['decl_roster'] - dsea['bias'])
    registered_roster_residual = recovered_registered - (env['decl_roster'] - dsea['bias'])

    findings = []
    if env['decl_records'] - dsea['bias'] != len(records):
        findings.append('record count residual %d: the envelope declares %d, %d recovered'
                        % (len(records) - (env['decl_records'] - dsea['bias']),
                           env['decl_records'] - dsea['bias'], len(records)))
    if env['decl_segments'] - dsea['bias'] != len(dsea['segments']):
        findings.append('segment count residual %d'
                        % (len(dsea['segments']) - (env['decl_segments'] - dsea['bias'])))
    if registered_roster_residual:
        findings.append(
            'roster size residual %d: the envelope declares %d registered squads, '
            '%d registered squads recovered'
            % (registered_roster_residual, env['decl_roster'] - dsea['bias'],
               recovered_registered))
    if unregistered:
        findings.append('unregistered squads on the wire: %s' % ', '.join(unregistered))
    if attribution_residual:
        findings.append(
            'attribution residual %d: season gross output %d less the conference '
            'gross outputs %d, carried by %s'
            % (attribution_residual, season_gross, conf_gross,
               ', '.join(unregistered) if unregistered else 'no squad'))
    if absent:
        findings.append('absent entrants registered but never on the wire: %s'
                        % ', '.join(absent))
    if dsea['backward_ticks']:
        findings.append('%d places where ticks move backwards inside a segment'
                        % dsea['backward_ticks'])

    def capture_block(dec, role, name):
        e = dec['envelope']
        return {
            'role': role, 'file': name, 'capture_id': e['capture_id'],
            'guard_register': e['guard_register'],
            'guards_active': [g for g in GUARD_NAMES if e['guards'][g]],
            'count_bias': dec['bias'], 'base_tick': e['base_tick'],
            'envelope_checksum_ok': e['checksum_ok'],
            'segment_checksums_ok': all(s['checksum_ok'] for s in dec['segments']),
            'declared_segments': e['decl_segments'] - dec['bias'],
            'recovered_segments': len(dec['segments']),
            'declared_records': e['decl_records'] - dec['bias'],
            'recovered_records': len(dec['records']),
            'declared_roster_size': e['decl_roster'] - dec['bias'],
            'distinct_squads_recovered': len(set(r['squad_key']
                                                 for r in dec['records'])),
            'backward_ticks': dec['backward_ticks'],
        }

    data_quality = {
        'capture': capture_block(dsea, 'season', season['name']),
        'declared': {'segments': env['decl_segments'] - dsea['bias'],
                     'records': env['decl_records'] - dsea['bias'],
                     'roster_size': env['decl_roster'] - dsea['bias']},
        'recovered': {'segments': len(dsea['segments']),
                      'records': len(records),
                      'distinct_squads': len(agg),
                      'registered_squads': recovered_registered,
                      'unregistered_squads': len(unregistered)},
        'residuals': {
            'segment_count': len(dsea['segments']) - (env['decl_segments'] - dsea['bias']),
            'record_count': len(records) - (env['decl_records'] - dsea['bias']),
            'roster_size': registered_roster_residual,
            'distinct_squads_over_declared_roster': roster_residual,
            'attribution': attribution_residual,
            'segment_attribution': segment_residual,
            'output_ledger': (season_gross + season_voided)
                             - sum(r['value'] for r in records if r['kind'] == 2),
            'upkeep_ledger': (season_charged + season_overflow) - season_raw,
            'backward_ticks': dsea['backward_ticks'],
        },
        'kind_ledger': {n: ledger.get(n, 0) for n in
                        ['deploy', 'output', 'eliminate', 'disband', 'concede',
                         'transfer', 'close']},
        'unregistered_squads': [
            {'squad_key': k, 'gross_output': agg[k]['gross_output'],
             'voided_output': agg[k]['voided_output'],
             'raw_upkeep': agg[k]['raw_upkeep'],
             'charged_upkeep': agg[k]['charged_upkeep'],
             'overflow_upkeep': agg[k]['overflow_upkeep'],
             'matches': agg[k]['matches'],
             'season_score': agg[k]['season_score'],
             'standing_points': agg[k]['standing_points'],
             'tier': agg[k]['tier']} for k in unregistered],
        'absent_entrants': [{'squad_key': k,
                             'entrant_id': league.entrant_id(k),
                             'division': league.division(k),
                             'conference': league.conference(k),
                             'licence_class': league.licence_class(k),
                             'segment': league.segment(k)} for k in absent],
        'other_captures': {
            role: capture_block(dec, role, corpus['captures'][role]['name'])
            for role, dec in [('reference', dref), ('control', dctl)] if dec
        },
        'baseline_transcript_check': transcript_report,
        'findings': findings,
    }

    policy_audit = {
        'resolved': {'engine_tag': engine_tag, 'window_depth': window_depth,
                     'sentinel': '0x%02X' % sentinel, 'season_budget': budget,
                     'promotion_threshold': threshold},
        'unstated_quantities': {
            'guard_quantities': {
                'rosters': {'engine_tag': tag_roster,
                            'window_depth': window_roster,
                            'sentinel': ['0x%02X' % s for s in sentinel_roster]},
                'candidate_space_size': len(combos),
                'surviving_readings': [
                    {'engine_tag': t, 'window_depth': w, 'sentinel': '0x%02X' % s}
                    for t, w, s in survivors],
                'candidates': [
                    {'engine_tag': a['engine_tag'], 'window_depth': a['window_depth'],
                     'sentinel': '0x%02X' % a['sentinel'], 'verdict': a['verdict'],
                     'control_figures_reproduced': a['matched'],
                     'falsifier': a['falsifier'],
                     'decode_error': a.get('error')} for a in attempts],
            },
            'season_budget': {'roster': budget_roster,
                              'candidate_space_size': len(budget_roster),
                              'candidates': budget_attempts,
                              'resolved': budget},
            'promotion_threshold': {'roster': threshold_roster,
                                    'candidate_space_size': len(threshold_roster),
                                    'candidates': threshold_attempts,
                                    'resolved': threshold},
        },
        'attribution_settlement_passes': passes,
        'hand_overs': [{'from': k, 'to': v} for k, v in sorted(handed.items())],
        'effective_owners': {k: owner[k] for k in sorted(owner) if owner[k] != k},
        'bracket': {
            'eligibility': {'own_effective_owner': True,
                            'match_floor': policy['match_floor']},
            'eligible_pool': bracket['pool'],
            'candidate_space_size': bracket['candidate_space'],
            'best_satisfied_constraint_count': bracket['best_satisfied'],
            'constraints': BRACKET_CONSTRAINTS,
            'elected': bracket['members'],
            'elected_satisfied': bracket['satisfied'],
            'elected_constraint_verdicts': [
                {'constraint': c, 'satisfied': bool(v)}
                for c, v in zip(BRACKET_CONSTRAINTS, bracket['checks'])],
            'elected_charged_upkeep': bracket['charged_upkeep'],
            'thresholds': {k: policy[k] for k in
                           ['bracket_size', 'prominence', 'standing_floor',
                            'weight_floor', 'gross_floor']},
        },
        'docket': {'size': policy['docket_size'],
                   'conference_cap': policy['docket_cap'],
                   'review_weight': policy['review_weight']},
        'calibration': calibration,
        'calibration_all_agree': all(c['agrees'] for c in calibration),
        'baseline_transcript_check': transcript_report,
    }

    summary = {
        'capture': capture_block(dsea, 'season', season['name']),
        'resolved_policy': {'engine_tag': engine_tag, 'window_depth': window_depth,
                            'sentinel': '0x%02X' % sentinel,
                            'season_budget': budget,
                            'promotion_threshold': threshold,
                            'roster_dictionary': dictionary},
        'totals': {'matches': len(slices), 'records': len(records),
                   'squads': len(agg),
                   'registered_squads': recovered_registered,
                   'unregistered_squads': len(unregistered),
                   'gross_output': season_gross, 'voided_output': season_voided,
                   'raw_upkeep': season_raw, 'charged_upkeep': season_charged,
                   'overflow_upkeep': season_overflow,
                   'season_score': sum(r['season_score'] for r in agg.values()),
                   'standing_points': sum(r['standing_points'] for r in agg.values())},
        'kind_ledger': {n: ledger.get(n, 0) for n in
                        ['deploy', 'output', 'eliminate', 'disband', 'concede',
                         'transfer', 'close']},
        'tier_counts': {t: sum(1 for r in agg.values() if r['tier'] == t)
                        for t in TIER_ORDER},
        'standings': [
            {'rank': r['rank'], 'squad_key': r['squad_key'],
             'entrant_id': r['entrant_id'], 'registered': r['registered'],
             'division': r['division'], 'conference': r['conference'],
             'licence_class': r['licence_class'], 'segment': r['segment'],
             'matches': r['matches'], 'gross_output': r['gross_output'],
             'voided_output': r['voided_output'], 'raw_upkeep': r['raw_upkeep'],
             'charged_upkeep': r['charged_upkeep'],
             'overflow_upkeep': r['overflow_upkeep'],
             'match_awards': r['match_awards'], 'streak_credit': r['streak_credit'],
             'standing_points': r['standing_points'],
             'season_score': r['season_score'], 'tier': r['tier'],
             'effective_owner': r['effective_owner'],
             'handed_entry_to': r['handed_entry_to']} for r in standings],
        'matches': [
            {'logical_position': sl['logical'], 'match_id': sl['match_id'],
             'conference': match_conference(sl, league),
             'close_tick': sl['close_tick'],
             'records': len(sl['records']),
             'escape_records': sl['escape_records'],
             'gross_output': sum(v['gross'] for v in sl['stats'].values()),
             'voided_output': sum(v['voided'] for v in sl['stats'].values()),
             'raw_upkeep': sum(v['raw'] for v in sl['stats'].values()),
             'review_index': sum(v['voided'] for v in sl['stats'].values())
                             + policy['review_weight'] * sl['escape_records'],
             'participants': sorted(sl['stats']),
             'conceded': sorted(sl['conceded']),
             'winners': sorted(sl['winners'])} for sl in slices],
        'finals_bracket': {
            'members': [entry_row(k, agg[k], league) for k in bracket['members']],
            'satisfied_constraints': bracket['satisfied'],
            'best_possible_satisfied_constraints': bracket['best_satisfied'],
            'candidate_space_size': bracket['candidate_space'],
            'eligible_pool': bracket['pool'],
            'combined_charged_upkeep': bracket['charged_upkeep'],
            'combined_ladder_weight': sum(league.ladder_weight(k)
                                          for k in bracket['members']),
            'combined_gross_output': sum(agg[k]['gross_output']
                                         for k in bracket['members']),
        },
        'review_docket': {
            'matches': [{'match_id': e['match_id'],
                         'logical_position': e['logical'],
                         'conference': e['conference'],
                         'voided_output': e['voided_output'],
                         'escape_records': e['escape_records'],
                         'review_index': e['review_index']}
                        for e in docket['matches']],
            'total_review_index': docket['total_review_index'],
            'conference_spread': docket['conference_spread'],
            'size': len(docket['matches']),
            'conference_cap': policy['docket_cap'],
        },
        'attribution_settlement_passes': passes,
        'reconciliation_findings': findings,
    }

    write_json(out_dir, 'summary.json', summary)
    write_json(out_dir, 'data_quality.json', data_quality)
    write_json(out_dir, 'policy_audit.json', policy_audit)
    for conf, rep in conf_reports.items():
        write_json(out_dir, 'region_%s.json' % conf, rep)
    for seg, rep in seg_reports.items():
        write_json(out_dir, 'segment_%s.json' % seg, rep)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
