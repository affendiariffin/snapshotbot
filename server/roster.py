# Roster enrichment — turns raw Yellowscribe capture rows into something callable by name.
#
# Three jobs, all done ONCE here so the viewer, the analyze-game cruncher and the offline
# download all read the same strings (same reasoning as zones.tag_reserve_zones):
#
#   1. BACKFILL. The token only started sending `unit_name` / `keywords` as their own columns
#      on 2026-07-24; before that both were buried in the leader model's `unit_data` Lua blob,
#      and even now only the LEADER row carries that blob — the other models of the same unit
#      arrive bare. Derive what's missing and spread it across the whole unit.
#
#   2. LABEL. A list routinely fields several copies of one datasheet (obe_a7Ek ran 2x Exorcist,
#      2x Immolator, 2x Seraphim Squad), so `unit_name` is NOT unique and anything that groups by
#      it silently merges distinct units. Labels are made unique on purpose: datasheet name,
#      disambiguated by whatever wargear actually differs between the copies, else a stable
#      ordinal. Key everything by `unit_id`; never by name.
#
#   3. ABILITY TEXT. A model's Description lists its abilities by NAME only — the same
#      summary the hover tooltip shows. The full rules text lives in the leader's
#      `unitData.abilities` table (what Yellowscribe's own numpad-1 popup renders), which
#      was already being captured and then thrown away with the blob. Parsed out here into
#      `bundle["abilities"]`, keyed by unit_id: one copy per UNIT, not per model row, so the
#      wordiest army adds a few KB to the 5s poll rather than a few KB times nine Sacresants.
#
# The skill side keeps its own copy of this labelling rule in
# ~/.claude/skills/analyze-game/roster_util.py, because it must also work on archived games
# fetched from an older server. Change one, change the other.
import re

UNIT_NAME_RE = re.compile(r'^\s*unitName\s*=\s*"([^"]*)"', re.M)
KEYWORDS_RE = re.compile(r'^\s*keywords\s*=\s*"([^"]*)"', re.M)
# Lua string literal, either long-bracket ([[…]] / [=[…]=], which is what Yellowscribe emits
# for rules text — it contains quotes and apostrophes) or plain quoted. Written out twice
# because the long-bracket form needs a backreference to close on its own level count, and
# the group numbers shift when the pattern is used a second time in one regex.
_LUA_STR_1 = r'(?:\[(=*)\[(.*?)\]\1\]|"((?:[^"\\]|\\.)*)")'
_LUA_STR_2 = r'(?:\[(=*)\[(.*?)\]\4\]|"((?:[^"\\]|\\.)*)")'
# `desc =` appears ONLY in the abilities table — weapons carry `abilities=`/`d=` instead —
# so matching the name/desc pair anywhere in the blob needs no block-extent parsing (and
# can't be fooled by a `}` inside the rules text).
ABILITY_RE = re.compile(r'name\s*=\s*' + _LUA_STR_1 + r'\s*,\s*desc\s*=\s*' + _LUA_STR_2,
                        re.S)
BB_RE = re.compile(r"\[[0-9a-fA-F]{6}\]|\[-\]")
# Weapon names sit on their own line in a Yellowscribe Description, wrapped in one colour tag.
WEP_LINE_RE = re.compile(r"\[c6c930\]([^\[\]]+)\[-\]")
# Ubiquitous kit tells two squads apart in no way at all.
BORING = {"bolt pistol", "boltgun", "close combat weapon", "armoured tracks",
          "hunter-killer missile", "frag grenades", "krak grenades"}


def strip_bb(s):
    return BB_RE.sub("", s or "")


def _distinctive_weapons(rows):
    out = set()
    for r in rows:
        for w in WEP_LINE_RE.findall(r.get("descr") or ""):
            w = w.strip()
            if w and w.lower() not in BORING:
                out.add(w)
    return out


def _lua_str(long_body, quoted):
    if long_body is not None:
        return long_body
    if quoted is None:
        return None
    return quoted.replace('\\"', '"').replace("\\\\", "\\")


def parse_abilities(blob):
    out, seen = [], set()
    for m in ABILITY_RE.finditer(blob or ""):
        name = (_lua_str(m.group(2), m.group(3)) or "").strip()
        desc = (_lua_str(m.group(5), m.group(6)) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"n": name, "d": desc})
    return out


def enrich_rosters(bundle):
    rows = bundle.get("rosters") or []
    if not rows:
        return bundle
    by_unit = {}
    for r in rows:
        by_unit.setdefault(r.get("unit_id"), []).append(r)

    name_of, kw_of, base_of, abil_of = {}, {}, {}, {}
    for u, urows in by_unit.items():
        name = next((r.get("unit_name") for r in urows if r.get("unit_name")), None)
        kws = next((r.get("keywords") for r in urows if r.get("keywords")), None)
        for r in urows:
            blob = r.get("unit_data") or ""
            if blob and not abil_of.get(u):
                abil_of[u] = parse_abilities(blob)
            if not name:
                m = UNIT_NAME_RE.search(blob)
                name = m.group(1) if m else name
            if not kws:
                m = KEYWORDS_RE.search(blob)
                kws = m.group(1) if m else kws
        name_of[u] = name
        kw_of[u] = kws
        if not name:
            # No Yellowscribe unit name anywhere (unscripted leader, or a hand-built model):
            # the most common model name is the honest fallback, tie-broken by name so it can't
            # depend on row order. It feeds the LABEL only — `unit_name` stays null, because
            # consumers use its presence to tell a captured datasheet from a mesh nickname.
            counts = {}
            for r in urows:
                counts[r.get("model_name") or "?"] = counts.get(r.get("model_name") or "?", 0) + 1
            name = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        base_of[u] = name

    # Collisions resolve per side — the mirror match is a real thing.
    groups = {}
    for u, urows in by_unit.items():
        side = next((r.get("team") for r in urows if r.get("team")), None)
        groups.setdefault((side, base_of[u]), []).append(u)

    label_of = {}
    for (_side, name), units in groups.items():
        if len(units) == 1:
            label_of[units[0]] = name
            continue
        weps = {u: _distinctive_weapons(by_unit[u]) for u in units}
        shared = set.intersection(*weps.values()) if weps else set()
        for i, u in enumerate(sorted(units)):          # sorted => stable across requests
            uniq = sorted(weps[u] - shared)
            label_of[u] = f"{name} ({', '.join(uniq[:2])})" if uniq else f"{name} #{i + 1}"

    for r in rows:
        u = r.get("unit_id")
        r["unit_name"] = name_of.get(u)
        r["keywords"] = kw_of.get(u)
        r["label"] = label_of.get(u)
    # Per UNIT, not per row — the tooltip is keyed by unit_id + model name, but the rules
    # text is the same for every model of the unit.
    bundle["abilities"] = {u: a for u, a in abil_of.items() if u and a}
    return bundle
