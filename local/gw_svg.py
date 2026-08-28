# SVG emitter for the GW-PDF layouts (P3). Turns extract_gw_layouts.py's world-inch geometry
# into the 45 files server/static/layouts/<KEY>.svg, in the SHAPE the rest of snapshotbot
# already parses: viewBox 1200x880 at 20px/in, a <g id="terrain-layer">, a
# <g id="territory-line-layer"> holding a single <line>, and deployment polygons in
# rgba(255,74,74) / rgba(74,158,255). Keep those four things or zones.py's zone parse and
# rebuild_layouts_meta.territory_line() both go quiet-wrong rather than loudly broken.
#
# OBJECTIVE ICONS ARE INHERITED, NOT REDRAWN (Fendi, 2026-08-28).
# The icon carries meaning -- home vs central vs expansion -- so it has to survive the source
# swap. These are the exact shapes the current SVGs use, reproduced here rather than
# reinvented, so the markers do not shift visually when the geometry changes underneath:
#     home      circle r=17, castle glyph, owner-coloured  #c0392b attacker / #2e6da4 defender
#     central   circle r=17, skull glyph,  #1f8a70
#     expansion 34x34 rounded square rotated 45 (diamond), skull glyph, #27ae60
# All four carry a white 3px stroke and the glyph is scaled 1.3076923 about the badge centre.
# Counts across the 45 current layouts: 45 attacker home, 45 defender home, 60 central,
# 90 expansion = 240, which is the objective total rebuild_layouts_meta.py expects.
# GW's own print colours are darker (#9E0A0E / #003E68 / #007360 / #219184); we keep the
# snapshotbot palette deliberately, because the point of inheriting is visual continuity.
#
# The data-objective-* attributes are inherited too -- they are how a reader (and any future
# tooling) tells the three kinds apart once the icon is rasterised into a thumbnail.

PPI = 20.0
GLYPH_SCALE = 1.3076923076923077
BADGE_R = 17.0

OWNER_FILL = {"red": "#c0392b", "blue": "#2e6da4"}
KIND_FILL = {"central": "#1f8a70", "expansion": "#27ae60"}
# extract_gw_layouts calls the sides red/blue; the SVG has always said attacker/defender.
OWNER_ATTR = {"red": "attacker", "blue": "defender"}


def svg_xy(x, z):
    return ((x + 30.0) * PPI, (22.0 - z) * PPI)


def poly_points(poly):
    """World-inch ring -> an SVG points string with the first vertex REPEATED at the end.

    The repeat is not decoration. A traced terrain-area outline can leave its first and last
    vertex a long way apart -- the 7"x11.5" areas close over a 7.03in edge -- and while the SVG
    spec says <polygon> closes that edge when stroking, MuPDF does not draw it. Firefox does, so
    the Playwright PNG bake and the browser viewer were never going to show this, which is
    exactly why it is worth pinning: the one renderer that disagrees is the one used for offline
    previews, and a border that only goes missing in some tools is the kind of thing that gets
    chased twice. Closing explicitly costs one vertex and removes the dependency.
    """
    ring = list(poly) + [poly[0]]
    return " ".join("%.1f,%.1f" % svg_xy(x, z) for x, z in ring)


def _skull(cx, cy, bg):
    return ('<g fill="#fff">'
            '<circle cx="%(cx).1f" cy="%(top).1f" r="5.5"></circle>'
            '<rect x="%(lx).1f" y="%(jy).1f" width="6" height="3.6" rx="1.3"></rect>'
            '<circle cx="%(ex).1f" cy="%(top).1f" r="1.6" fill="%(bg)s"></circle>'
            '<circle cx="%(ex2).1f" cy="%(top).1f" r="1.6" fill="%(bg)s"></circle>'
            '</g>') % {"cx": cx, "top": cy - 1.5, "lx": cx - 3, "jy": cy + 2.5,
                       "ex": cx - 2, "ex2": cx + 2, "bg": bg}


def _castle(cx, cy, bg):
    return ('<g fill="#fff">'
            '<rect x="%(lx).1f" y="%(by).1f" width="12" height="8" rx="0.5"></rect>'
            '<rect x="%(lx).1f" y="%(ty).1f" width="3" height="4"></rect>'
            '<rect x="%(mx).1f" y="%(ty).1f" width="3" height="4"></rect>'
            '<rect x="%(rx).1f" y="%(ty).1f" width="3" height="4"></rect>'
            '<rect x="%(mx).1f" y="%(dy).1f" width="3" height="5" fill="%(bg)s"></rect>'
            '</g>') % {"lx": cx - 6, "mx": cx - 1.5, "rx": cx + 3, "by": cy - 1,
                       "ty": cy - 5, "dy": cy + 2, "bg": bg}


def objective_icon(kind, owner, x, z, number=None):
    cx, cy = svg_xy(x, z)
    if kind == "home":
        bg = OWNER_FILL[owner]
        badge = ('<circle cx="%.1f" cy="%.1f" r="%.0f" fill="%s" stroke="#fff" '
                 'stroke-width="3"></circle>' % (cx, cy, BADGE_R, bg))
        glyph = _castle(cx, cy, bg)
    else:
        bg = KIND_FILL[kind]
        if kind == "expansion":
            badge = ('<rect x="%.1f" y="%.1f" width="34" height="34" rx="2" '
                     'transform="rotate(45 %.1f %.1f)" fill="%s" stroke="#fff" '
                     'stroke-width="3"></rect>'
                     % (cx - BADGE_R, cy - BADGE_R, cx, cy, bg))
        else:
            badge = ('<circle cx="%.1f" cy="%.1f" r="%.0f" fill="%s" stroke="#fff" '
                     'stroke-width="3"></circle>' % (cx, cy, BADGE_R, bg))
        glyph = _skull(cx, cy, bg)
    scaled = ('<g transform="translate(%.1f %.1f) scale(%s) translate(%.1f %.1f)">%s</g>'
              % (cx, cy, GLYPH_SCALE, -cx, -cy, glyph))
    return ('<g class="terrain-objective" data-objective-type="%s" data-objective-owner="%s"'
            ' data-objective-number="%s"><g pointer-events="none">%s%s</g></g>'
            % (kind, OWNER_ATTR.get(owner, ""), "" if number is None else number, badge,
               scaled))


EYE_R = 18.0                # SVG units; GW prints these at ~2.05in, objectives at ~3.3in
EYE_OPACITY = 0.5           # Fendi, 2026-08-28 -- present but not competing with the board
EYE_GREY = "#808080"


def eye_icon(kind, x, z):
    """Single / Separate Terrain Area marker.

    These are not decoration: an OPEN eye means the two footprints either side are ONE terrain
    area, a CROSSED one means they are two. That decides what counts as a single obscuring
    region, so it changes what a model is wholly within. Drawn at 50% so the board reads first.
    Unlike the objective badges there is nothing to inherit -- rapidingress never drew these.
    """
    cx, cy = svg_xy(x, z)
    lid = ('<path d="M %.1f %.1f Q %.1f %.1f %.1f %.1f Q %.1f %.1f %.1f %.1f Z" fill="#fff">'
           '</path>' % (cx - 8.5, cy, cx, cy - 7.5, cx + 8.5, cy,
                        cx, cy + 7.5, cx - 8.5, cy))
    parts = ('<circle cx="%.1f" cy="%.1f" r="%.0f" fill="%s" stroke="#fff" stroke-width="2.5">'
             '</circle>%s<circle cx="%.1f" cy="%.1f" r="3.1" fill="%s"></circle>'
             % (cx, cy, EYE_R, EYE_GREY, lid, cx, cy, EYE_GREY))
    if kind == "separate":
        parts += ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#fff" '
                  'stroke-width="3.4" stroke-linecap="round"></line>'
                  % (cx - 12, cy + 12, cx + 12, cy - 12))
    return ('<g class="terrain-eye" data-eye="%s" opacity="%s" pointer-events="none">%s</g>'
            % (kind, EYE_OPACITY, parts))


def eyes_layer(markers):
    return ('<g id="terrain-eyes-layer">%s</g>'
            % "".join(eye_icon(m["kind"], m["at"][0], m["at"][1]) for m in markers))


def objectives_layer(objs):
    return ('<g id="objectives-layer">%s</g>'
            % "".join(objective_icon(o["kind"], o.get("owner"), o["at"][0], o["at"][1], i + 1)
                      for i, o in enumerate(objs)))
