#!/usr/bin/env python3
"""Build the profile: render every SVG and the README from content/profile.json.

  python -I scripts/build.py                build (refreshes the contribution calendar if GITHUB_TOKEN is set)
  python -I scripts/build.py --check       fail on invented numbers, banned words, bad SVGs, stale output
  python -I scripts/build.py --check-links  fail on any link that does not answer 200

Stdlib only. The fonts were subset once with fonttools (not needed to build):
  fonttools varLib.instancer <variable.ttf> wght=700 -o x.ttf
  pyftsubset x.ttf --unicodes=U+0020-007E,U+00A0,U+00B7,U+2011,U+2018-201D,U+2192,U+2713 --flavor=woff2
assets/fonts/metrics.json holds each glyph's advance width in em, so text can be measured here.
"""
import base64
import datetime
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P = json.loads((ROOT / "content/profile.json").read_text("utf-8"))
M = json.loads((ROOT / "assets/fonts/metrics.json").read_text("utf-8"))

# Brand tokens (D-21). Teal = cheap, safe, the result. Amber = costly, risky, in progress.
C = dict(ground="#0b0f14", dots="#18212d", line="#1f2a37", axis="#2a3646", gutter="#5b6878",
         ink="#e6edf3", muted="#8b98a8", teal="#2dd4bf", amber="#f5b544")
FONT = {  # class -> (file, css descriptors, fallback stack)
    "serif": ("playfair-700", "font-weight:700", "Georgia,serif"),
    "ital": ("playfair-700i", "font-weight:700;font-style:italic", "Georgia,serif"),
    "sans": ("inter-400", "font-weight:400", "system-ui,sans-serif"),
    "mono": ("jetbrainsmono-400", "font-weight:400", "ui-monospace,Consolas,monospace"),
    "goth": ("leaguegothic", "font-weight:400", "Impact,'Arial Narrow',sans-serif"),
}
EASE = "cubic-bezier(.2,.8,.2,1)"
SVG_NS = "http://www.w3.org/2000/svg"
MAX_SVG, MAX_TOTAL = 150_000, 1_500_000

BANNED = ["fosters", "landscape", "delve", "invaluable", "enlightening", "in the realm of", "elevate",
          "enhance", "tapestry", "crafting", "in the digital era", "in the world of", "unsung hero",
          "secret weapon", "cultivate", "this is gold", "game changer", "game-changer", "fluff",
          "excited to share", "thrilled to announce", "happy to share", "key takeaways", "deep dive",
          "unlock", "journey"]
FORBIDDEN = ["attention hero", "attentionhero", "melting-gpu", "fumi", "playfumi", "fumiverse", "skm_ahmed",
             "looking for", "open to work", "aws certified", "twitter", "x.com"]

PROBLEMS = []     # layout overflows found while rendering
COMPUTED = set()  # numbers this script produced itself (counts, API data)
esc = html.escape


def width(s, font, size):
    m = M[FONT[font][0]]
    return sum(m.get(ch, m["x"]) for ch in s) * size


def text(x, y, spans, size, font="sans", fill="ink", anchor="start", maxw=None, cls="", style=""):
    """One line of text. spans is a string, or a list of (string, fill[, font])."""
    if isinstance(spans, str):
        spans = [(spans, fill)]
    spans = [(s, f, (rest[0] if rest else font)) for s, f, *rest in spans]
    w = sum(width(s, fn, size) for s, _, fn in spans)
    if maxw and w > maxw:
        PROBLEMS.append(f"text overflows by {w - maxw:.0f}px: {''.join(s for s, _, _ in spans)!r}")
    body = "".join(f'<tspan class="{fn}" fill="{C[f]}">{esc(s)}</tspan>' for s, f, fn in spans)
    attrs = (f' text-anchor="{anchor}"' if anchor != "start" else "") + (f' class="{cls}"' if cls else "") \
        + (f' style="{style}"' if style else "")
    return f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" xml:space="preserve"{attrs}>{body}</text>'


def rise(delay, inner):
    return f'<g class="r" style="animation-delay:{delay:.2f}s">{inner}</g>'


def kicker(x, y, s, size=36):
    return text(x, y, [("// ", "teal"), (s, "muted")], size, "mono")


def tick(x, y, k=1.0):
    """A drawn check mark (the mono font has no U+2713), sized for text of 46px times k."""
    return (f'<path d="M{x + 3 * k:.1f} {y - 15 * k:.1f}l{9 * k:.1f} {11 * k:.1f} {17 * k:.1f} {-25 * k:.1f}" fill="none" '
            f'stroke="{C["teal"]}" stroke-width="{5 * k:.1f}" stroke-linecap="round" stroke-linejoin="round"/>')


def footer(w, h, left, right, font="sans"):
    return (f'<path d="M64 {h - 78}H{w - 64}" stroke="{C["line"]}" stroke-width="2"/>'
            + text(64, h - 34, left, 30, font, "muted") + text(w - 64, h - 34, right, 30, "mono", "teal", "end"))


def mix(a, b, t):
    """Blend hex colour a towards b by t. Shades stay inside the brand's hues."""
    ch = lambda c, i: int(c[i:i + 2], 16)
    return "#" + "".join(f"{round(ch(a, i) + (ch(b, i) - ch(a, i)) * t):02x}" for i in (1, 3, 5))


def gutter(gw, h, ys):
    nums = "".join(f'<text x="{gw - 16}" y="{y:.0f}" font-size="30" text-anchor="end" class="mono g" '
                   f'fill="{C["gutter"]}">{i:02d}</text>' for i, y in enumerate(ys, 1) if y < h - 24)
    return (f'<rect width="{gw}" height="{h}" fill="{C["ground"]}"/>'
            f'<path d="M{gw} 0V{h}" stroke="{C["line"]}" stroke-width="2"/>{nums}')


def svg(w, h, body, fonts, grid=True):
    """The shared card: rounded dark ground, 2px border, dot grid, embedded fonts, house motion."""
    css = ""
    for k in fonts:
        f, desc, fallback = FONT[k]
        data = base64.b64encode((ROOT / f"assets/fonts/{f}.woff2").read_bytes()).decode()
        # The family is named after the file: a family called "serif" would be read as the generic keyword.
        css += (f"@font-face{{font-family:'{f}';{desc};src:url(data:font/woff2;base64,{data}) format('woff2')}}"
                f".{k}{{font-family:'{f}',{fallback};{desc}}}")
    css += (
        ".serif,.ital{font-variant-numeric:lining-nums}.mono{font-variant-ligatures:none}"
        # House motion: the final frame is the default state. Each part arrives once and holds.
        f".r{{animation:r .5s {EASE} both}}@keyframes r{{from{{opacity:0;transform:translateY(36px)}}}}"
        f".f{{animation:f .5s {EASE} both}}@keyframes f{{from{{opacity:0}}}}"
        f".d{{stroke-dasharray:1;animation:d .6s {EASE} both}}@keyframes d{{from{{stroke-dashoffset:1}}}}"
        ".t{animation:t .8s both}@keyframes t{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}"
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
    )
    dots = (f'<pattern id="p" width="53" height="53" patternUnits="userSpaceOnUse">'
            f'<circle cx="26.5" cy="26.5" r="2.4" fill="{C["dots"]}"/></pattern>'
            f'<rect width="{w}" height="{h}" fill="url(#p)"/>') if grid else ""
    return (f'<svg xmlns="{SVG_NS}" viewBox="0 0 {w} {h}" width="{w}" height="{h}"><style>{css}</style>'
            f'<clipPath id="c"><rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="24"/></clipPath>'
            f'<g clip-path="url(#c)"><rect width="{w}" height="{h}" fill="{C["ground"]}"/>{dots}{body}</g>'
            f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="24" fill="none" stroke="{C["line"]}" '
            f'stroke-width="2"/></svg>\n')


def floor(w, h, horizon=118, vx=1490, pitch=150):
    """The brand's dot grid as a floor in perspective, receding to the top right.
    Each row is one dotted line: round caps on zero-length dashes draw circles."""
    rows = []
    for k in range(26):
        z = 1 + k * 0.14
        y = horizon + (h + 14 - horizon) / z
        step = pitch / z
        x1 = vx - (int(vx / step) + 2) * step
        fade = min(1, 1.25 * ((y - horizon) / (h - horizon)) ** .9)  # dissolve before the horizon
        rows.append(f'<path d="M{x1:.1f} {y:.1f}H{w + step:.0f}" stroke-width="{10 / z ** .8:.2f}" '
                    f'stroke-dasharray="0 {step:.2f}" opacity="{fade:.2f}"/>')
    return f'<g stroke="{C["axis"]}" stroke-linecap="round" fill="none">{"".join(rows)}</g>'


def hero():
    h, W, H, G, X = P["hero"], 1600, 530, 84, 132
    link = P["links"]["work"].split("//")[1]
    body = floor(W, H) + gutter(G, H, [96 + 62 * i for i in range(8)])
    body += text(X, 96, [("// ", "teal"), (h["kicker"], "muted")], 42, "mono", maxw=1000)
    body += text(X, 220, h["headline"], 100, "serif", maxw=W - X - 56)
    body += text(X, 334, h["accent"], 100, "ital", "teal", maxw=W - X - 56)
    body += (f'<path class="d" pathLength="1" d="M{X} 366h{width(h["accent"], "ital", 100):.0f}" '
             f'stroke="{C["teal"]}" stroke-width="4" style="animation-delay:.3s"/>')
    body += f'<path d="M{X} 410H{W - 56}" stroke="{C["line"]}" stroke-width="2"/>'
    a, n, b = h["proof"]
    body += rise(.9, text(X, 472, [(a, "ink"), (n, "amber"), (b, "ink")], 46, "sans", maxw=W - X - 56))
    body += text(W - 56, 96, link, 42, "mono", "teal", "end", cls="f", style="animation-delay:1.3s")
    alt = f'{h["headline"]} {h["accent"]} {a}{n}{b}. {link}'
    return svg(W, H, body, ["serif", "ital", "sans", "mono"], grid=False), alt


def run():
    r, W, G, X, FS, LH, Y0 = P["run"], 1600, 84, 124, 46, 66, 178
    cw = width("a", "mono", FS)
    lines = r["lines"]
    passed = sum(l["status"] == "pass" for l in lines)
    prog = len(lines) - passed
    COMPUTED.update({str(passed), str(prog), "0"})
    summary = [(f"{passed} passed", "teal")]
    if prog:
        summary += [(" · ", "muted"), (f"{prog} in progress", "amber")]
    summary += [(" · ", "muted"), ("0 invented facts", "ink")]

    tab_w = width(r["file"], "mono", 32) + 76
    body = (f'<rect width="{W}" height="76" fill="{C["ground"]}"/><path d="M0 76H{W}" stroke="{C["line"]}" '
            f'stroke-width="2"/><rect x="{G}" y="0" width="{tab_w:.0f}" height="76" fill="{C["line"]}"/>'
            f'<rect x="{G}" y="0" width="{tab_w:.0f}" height="4" fill="{C["teal"]}"/>')
    body += text(G + 38, 50, r["file"], 32, "mono")
    body += text(X, Y0, "$", FS, "mono", "muted")
    body += text(X + 2 * cw, Y0, r["command"], FS, "mono", maxw=W - X - 2 * cw - 50, cls="t",
                 style=f"animation-delay:.2s;animation-timing-function:steps({len(r['command'])})")
    for i, l in enumerate(lines):
        y, ok = Y0 + (i + 2) * LH, l["status"] == "pass"
        col = "teal" if ok else "amber"
        mark = tick(X, y) if ok else text(X, y, "~", FS, "mono", "amber")
        row = mark + text(X + 2 * cw, y, [(l["tag"].ljust(11), col), (l["text"], "ink")], FS, "mono",
                          maxw=W - X - 2 * cw - 50)
        body += rise(1.1 + .3 * i, row)
    ys = Y0 + (len(lines) + 3) * LH
    body += rise(1.3 + .3 * len(lines), text(X, ys, summary, FS, "mono", maxw=W - X - 50))

    yr = ys + 50  # RESULT row
    body += rise(1.6 + .3 * len(lines),
                 f'<path d="M{X} {yr}H{W - 56}" stroke="{C["line"]}" stroke-width="2"/>'
                 + text(X, yr + 62, "RESULT", 30, "mono", "teal", style="letter-spacing:.06em"))
    for i, s in enumerate(r["results"]):
        x, col = X + i * 480, "amber" if s.get("risk") else "teal"
        stat = (text(x, yr + 216, s["n"], 160, "goth", col) + text(x, yr + 266, s["tag"], 36, "mono", col)
                + text(x, yr + 312, s["sub"], 31, "sans", "muted", maxw=450))
        body += rise(1.8 + .3 * len(lines) + .25 * i, stat)
    H = yr + 366
    body = gutter(G, H, [Y0 + LH * i for i in range(40)]) + body
    done = "; ".join(l["text"] for l in lines if l["status"] == "pass")
    todo = "; ".join(l["text"] for l in lines if l["status"] != "pass")
    alt = (f'Terminal running {r["command"]}. Passed: {done}. ' + (f"In progress: {todo}. " if todo else "")
           + "".join(s for s, _ in summary) + ". Result: "
           + "; ".join(f'{s["n"]} {s["tag"].lstrip("@")}, {s["sub"]}' for s in r["results"]) + ".")
    return svg(W, H, body, ["mono", "goth", "sans"]), alt


LS = "letter-spacing:.06em"


def case_text(c):
    """A case as plain words: (context, decision, result). Used for alt text and the text version."""
    res = "; ".join(f'{s["n"]} {s["tag"][1:]}, {s["sub"]}' for s in c.get("stats", [])) or "; ".join(c["checks"])
    return " ".join(c["context"]), " ".join(c["decision"]), res + (f'; {c["note"]}' if c.get("note") else "")


def case(c):
    """A decision record: CONTEXT (amber), DECISION (teal), RESULT."""
    W, H, X, XR = 1600, 640, 64, 930
    body = kicker(X, 82, c["kicker"]) + text(X, 170, c["title"], 68, "serif", maxw=W - 2 * X)
    for label, key, col, y, delay in (("CONTEXT", "context", "amber", 226, .3), ("DECISION", "decision", "teal", 400, .7)):
        sec = (f'<rect x="{X}" y="{y}" width="6" height="134" fill="{C[col]}"/>'
               + text(X + 30, y + 24, label, 28, "mono", col, style=LS)
               + "".join(text(X + 30, y + 76 + 52 * i, s, 42, maxw=XR - X - 70) for i, s in enumerate(c[key])))
        body += rise(delay, sec)
    body += rise(1.1, f'<rect x="{XR}" y="226" width="6" height="308" fill="{C["teal"]}"/>'
                 + text(XR + 30, 250, "RESULT", 28, "mono", "teal", style=LS))
    x = XR + 30
    for i, s in enumerate(c.get("stats", [])):
        col = "amber" if s.get("risk") else "teal"
        body += rise(1.3 + .2 * i, text(x, 420, s["n"], 150, "goth", col) + text(x, 466, s["tag"], 32, "mono", col)
                     + text(x, 506, s["sub"], 28, "sans", "muted"))
        x += max(width(s["n"], "goth", 150), width(s["tag"], "mono", 32), width(s["sub"], "sans", 28)) + 44
    if x - 44 > W - X:
        PROBLEMS.append(f'case {c["id"]}: result row overflows by {x - 44 - W + X:.0f}px')
    for i, s in enumerate(c.get("checks", [])):
        y = 322 + 64 * i
        body += rise(1.3 + .2 * i, tick(XR + 30, y, .7) + text(XR + 76, y, s, 32, "mono", maxw=W - X - XR - 76))
    if c.get("note"):
        body += rise(1.9, text(XR + 30, 514, c["note"], 30, "mono", "amber"))
    body += footer(W, H, P["hero"]["byline"], c["href"].split("//")[-1])
    ctx, dec, res = case_text(c)
    return svg(W, H, body, ["serif", "sans", "mono", "goth"]), f'Case: {c["title"]}. Context: {ctx} Decision: {dec} Result: {res}.'


def principle():
    p, W, H, X = P["principle"], 1600, 330, 64
    quote = text(X, 128, ">", 60, "mono", "teal") + "".join(
        text(X + 76, 128 + 84 * i, s, 66, "ital", maxw=W - 2 * X - 76) for i, s in enumerate(p["lines"]))
    body = rise(.2, quote) + footer(W, H, p["source"], P["links"]["work"].split("//")[1], "mono")
    return svg(W, H, body, ["ital", "mono"]), " ".join(p["lines"]) + f' ({p["source"]})'


ACTIVITY = ROOT / "assets/svg/activity.json"  # cached calendar, so builds and checks work offline


def fetch_activity():
    q = ("query($l:String!){user(login:$l){contributionsCollection{contributionCalendar"
         "{totalContributions weeks{contributionDays{date contributionCount}}}}}}")
    req = urllib.request.Request(
        "https://api.github.com/graphql", json.dumps({"query": q, "variables": {"l": P["skyline"]["login"]}}).encode(),
        {"Authorization": "bearer " + os.environ["GITHUB_TOKEN"], "User-Agent": "profile-build"})
    cal = json.load(urllib.request.urlopen(req, timeout=30))["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    data = {"total": cal["totalContributions"],
            "weeks": [[[d["date"], d["contributionCount"]] for d in w["contributionDays"]] for w in cal["weeks"]]}
    ACTIVITY.write_text(json.dumps(data, separators=(",", ":")) + "\n", "utf-8", newline="\n")


def skyline():
    """The contribution calendar as a city: one block per day, drawn back to front, a week at a time."""
    s, act, W, H, X = P["skyline"], json.loads(ACTIVITY.read_text("utf-8")), 1600, 770, 86
    ax, ay, dx, dy, y0, hmax, g = 25.5, 3.2, 11, 9.5, 488, 150, .1
    days = [(d, n) for wk in act["weeks"] for d, n in wk]
    active = sorted(n for _, n in days if n) or [1]
    p95 = active[int(.95 * (len(active) - 1))]  # cap, so one huge day does not flatten the rest
    longest = cur = 0
    for _, n in days:
        cur = cur + 1 if n else 0
        longest = max(longest, cur)
    best_day, best = max(days, key=lambda x: x[1])
    when = datetime.date.fromisoformat(best_day)
    stats = [(f'{act["total"]:,}', s["tags"][0], s["subs"][0]), (str(longest), s["tags"][1], s["subs"][1]),
             (str(best), s["tags"][2], f"{when.day} {when:%b %Y}")]
    COMPUTED.update(nums(" ".join(f"{n} {sub}" for n, _, sub in stats)))

    o = lambda u, v: (u * ax - v * dx, u * ay + v * dy)  # cell coordinates to screen offset
    (ex, ey), (fx, fy) = o(1 - 2 * g, 0), o(0, 1 - 2 * g)
    tile = "M{:.1f} {:.1f}l{:.1f} {:.1f} {:.1f} {:.1f} {:.1f} {:.1f}z".format(*o(g, g), ex, ey, fx, fy, -ex, -ey)
    top, left, right = C["teal"], mix(C["teal"], C["ground"], .38), mix(C["teal"], C["ground"], .62)
    body = f'<defs><path id="t" d="{tile}"/></defs>' + kicker(X, 80, s["kicker"])
    for i, (n, tag, sub) in enumerate(stats):
        x = X + 480 * i
        body += (text(x, 222, n, 130, "goth", "teal") + text(x, 268, tag, 34, "mono", "teal")
                 + text(x, 308, sub, 28, "sans", "muted"))
    for w, wk in enumerate(act["weeks"]):
        cells = ""
        for d, n in wk:
            v = datetime.date.fromisoformat(d).isoweekday() % 7  # Sunday is row 0, as on the profile
            px, py = -v * dx, v * dy
            if not n:
                cells += f'<use href="#t" x="{px:.1f}" y="{py:.1f}"/>'
                continue
            h = 10 + (hmax - 10) * min(n, p95) / p95
            (lx, ly), (rx, ry) = o(g, 1 - g), o(1 - g, g)
            cells += (f'<path fill="{left}" d="M{px + lx:.1f} {py + ly:.1f}l{ex:.1f} {ey:.1f}v{-h:.1f}l{-ex:.1f} {-ey:.1f}z"/>'
                      f'<path fill="{right}" d="M{px + rx:.1f} {py + ry:.1f}l{fx:.1f} {fy:.1f}v{-h:.1f}l{-fx:.1f} {-fy:.1f}z"/>'
                      f'<use href="#t" x="{px:.1f}" y="{py - h:.1f}" fill="{top}"/>')
        body += (f'<g class="r" style="animation-delay:{.2 + .025 * w:.3f}s"><g fill="{C["line"]}" '
                 f'transform="translate({X + 7 * dx + w * ax:.1f} {y0 + w * ay:.1f})">{cells}</g></g>')
    return svg(W, H, body, ["mono", "goth", "sans"]), s["alt"]


def toolbelt():
    t, W, X = P["toolbelt"], 1600, 64
    H = 222 + 58 * (max(map(len, t["rows"].values())) - 1) + 62
    body = kicker(X, 82, t["kicker"])
    for i, (tag, items) in enumerate(t["rows"].items()):
        x = X + i * 500
        body += rise(.2 + .25 * i, text(x, 158, tag, 38, "mono", "teal") + "".join(
            text(x, 222 + 58 * j, it, 42, "mono", maxw=470) for j, it in enumerate(items)))
    alt = "Toolbelt. " + " ".join(f'{tag[1:]}: {", ".join(items)}.' for tag, items in t["rows"].items())
    return svg(W, H, body, ["mono"]), alt


def render_all():
    """Every generated file as {relative path: content}."""
    out, L = {}, P["links"]
    work = f'[{L["work"].split("//")[1]}]({L["work"]})'

    def img(name, made, href=""):
        out[f"assets/svg/{name}.svg"], alt = made
        tag = f'<img src="assets/svg/{name}.svg" width="100%" alt="{esc(alt)}">'
        return f'<a href="{href}">{tag}</a>' if href else tag

    cards, plain = [], []
    for c in P["cases"]:
        cards.append(img(f'case-{c["id"]}', case(c), c["href"]))
        ctx, dec, res = case_text(c)
        title = f'[{c["title"]}]({c["href"]})' if c["href"] else c["title"]
        plain.append(f"**{title}**  \nContext: {ctx}  \nDecision: {dec}  \nResult: {res}.")
    out["README.md"] = "\n\n".join([
        img("hero", hero(), L["work"]),
        f'{P["about"]["text"]} Write-ups of the work: {work}',
        P["about"]["built"],
        img("run", run()),
        P["run"]["note"],
        "## Selected work",
        *cards,
        "<details><summary>Read the cases as text</summary>",
        *plain,
        "</details>",
        img("principle", principle(), P["principle"]["href"]),
        img("skyline", skyline()),
        img("toolbelt", toolbelt()),
        f"> Write-ups of the work: {work}",
        f'[syedkm.com]({L["site"]}) · [LinkedIn]({L["linkedin"]}) · [Instagram]({L["instagram"]})',
    ]) + "\n"
    return out


# ---- checks ---------------------------------------------------------------------------------

def nums(s):
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", s)}


def leaves(o, skip=("fact", "facts", "href", "id", "links", "_note")):
    """Every string of copy under o, leaving out references and URLs."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            if k not in skip:
                yield from leaves(v, skip)
    elif isinstance(o, list):
        for v in o:
            yield from leaves(v, skip)


def copy_items():
    """(label, copy, the F-IDs it cites) for every piece of copy in profile.json."""
    yield "hero", P["hero"], P["hero"]["facts"]
    yield "about", P["about"], P["about"]["facts"]
    for l in P["run"]["lines"] + P["run"]["results"]:
        yield f'run "{l.get("text") or l["n"]}"', l, [l["fact"]]
    for c in P["cases"]:
        yield f'case {c["id"]}', c, [c["fact"]]
    yield "principle", P["principle"], [P["principle"]["fact"]]
    yield "toolbelt", P["toolbelt"], [P["toolbelt"]["fact"]]
    yield "skyline", P["skyline"], []  # its numbers come from the API at build time, never from copy


def check_words(label, s, errs):
    low = s.lower()
    if "—" in s or "–" in s:
        errs.append(f"{label}: dash character in {s[:60]!r}")
    if re.search("[☀-✒✔-➿️\U0001f000-\U0001faff]", s):
        errs.append(f"{label}: emoji in {s[:60]!r}")
    for w in BANNED + FORBIDDEN:
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low):
            errs.append(f"{label}: banned or forbidden term {w!r}")


def check():
    errs, facts, allow = [], P["facts"], set(P["allow"])
    for label, item, ids in copy_items():
        ok = allow.union(*(facts[i]["numbers"] for i in ids))
        for s in leaves(item):
            for n in sorted(nums(s) - ok):
                errs.append(f"{label}: number {n} is not in {', '.join(ids)}")
    for s in leaves(P, skip=("links", "href")):
        check_words("profile.json", s, errs)

    files = render_all()  # fills COMPUTED and PROBLEMS
    errs += PROBLEMS
    known = allow.union(COMPUTED, *(f["numbers"] for f in facts.values()))
    total = 0
    for rel, want in files.items():
        path = ROOT / rel
        got = path.read_text("utf-8") if path.exists() else None
        if got != want:
            errs.append(f"{rel}: missing or stale, run the build")
            continue
        if rel.endswith(".svg"):
            size = len(got.encode())
            total += size
            if size > MAX_SVG:
                errs.append(f"{rel}: {size} bytes, over {MAX_SVG}")
            for url in set(re.findall(r"https?://[^\s\"')<]+", got)) - {SVG_NS}:
                errs.append(f"{rel}: external URL {url}")
            try:
                shown = " ".join("".join(t.itertext()) for t in ET.fromstring(got).iter(f"{{{SVG_NS}}}text")
                                 if "g" not in t.get("class", "").split())
            except ET.ParseError as e:
                errs.append(f"{rel}: not well-formed XML ({e})")
                continue
        else:  # README: alt text plus visible text, without tags and link targets
            shown = html.unescape(" ".join(re.findall(r'alt="([^"]*)"', got)) + " "
                                  + re.sub(r"\]\([^)]*\)", "]", re.sub(r"<[^>]+>", " ", got)))
            for src in re.findall(r'src="([^"]+)"', got):
                if not src.startswith("assets/"):
                    errs.append(f"{rel}: image not served from this repo: {src}")
            check_words(rel, shown, errs)
        for n in sorted(nums(shown) - known):
            errs.append(f"{rel}: renders number {n}, which no fact supplies")
    if total > MAX_TOTAL:
        errs.append(f"SVGs total {total} bytes, over {MAX_TOTAL}")
    for e in errs:
        print("FAIL", e)
    print(f"{len(errs)} problems" if errs else
          f"{len(files)} files, {total / 1000:.0f} KB of SVG, 0 problems, 0 invented facts")
    return 1 if errs else 0


def check_links():
    urls = sorted({s for s in leaves(P, skip=()) if s.startswith("http")}
                  | set(re.findall(r"https?://[^\s\"')<]+", (ROOT / "README.md").read_text("utf-8"))))
    bad = 0
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            code = urllib.request.urlopen(req, timeout=20).status
        except Exception as e:  # report every failure, then fail once
            code = getattr(e, "code", e)
        print(code, u)
        bad += code != 200
    return 1 if bad else 0


def main():
    if "--check" in sys.argv:
        return check()
    if "--check-links" in sys.argv:
        return check_links()
    if os.environ.get("GITHUB_TOKEN"):
        fetch_activity()
    for rel, content in render_all().items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8", newline="\n")
        print(f"{len(content.encode()) / 1000:6.1f} KB  {rel}")
    for p in PROBLEMS:
        print("WARN", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
