#!/usr/bin/env python3
"""
BLADE HUD - an ink-and-steel HUD overlay for AKAYUKI (Roblox), styled after the
Phantom Blade Zero HUD. It hides the game's own bars and draws its own, driven live by
what the game's bars show. It is read-only: it only looks at the screen, never touches the
game or sends input.

    python blade_hud.py                      run it over Roblox (borderless/windowed)
    python blade_hud.py --demo               fake values, so you can see every animation
    python blade_hud.py --preview shot.png   draw the HUD onto a screenshot -> hud_preview.png
    python blade_hud.py --video clip.mp4     run the HUD over a recording -> hud_video.mp4
                                             (needs: pip install opencv-python-headless)
    python blade_hud.py --calibrate          print what it reads from the game, 10 s

Keys while running:  F6 = show / hide the HUD (shows the game's own bars again)
                     Ctrl+F6 = quit        (Ctrl+C in the terminal also quits)

What the game shows -> what this HUD shows
    20 HP squares (bottom)      -> long steel health bar beside the crest diamond, with a
                                   crimson "chip" that drains after a hit, a shake, and a
                                   pulse at low health. The number under the crest = squares.
    yellow poise line           -> gold poise bar under the health bar; flares as it fills
                                   and throbs when your poise is about to break.
    2 dash pills                -> two dash diamonds (bottom right) that fill from the
                                   bottom while recharging and pop with a flash when ready.
    lock-on (top of screen)     -> the Q diamond glows crimson while you are locked on.
    F (kick)                    -> the F diamond flashes when you kick.
The HUD dims when nothing is happening and fades out on death / menu screens.

Needs Roblox in WINDOWED or BORDERLESS mode (overlays can't draw over exclusive
fullscreen). Windows 10 2004 or newer: the HUD hides itself from screen captures so it can
keep reading the real bars underneath it - so OBS will record the original game HUD.
"""
import argparse
import math
import pathlib
import random
import sys
import threading
import time
from collections import deque

try:
    import numpy as np
    import mss
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError as e:
    print(f"Missing package ({e}). Run:  pip install numpy mss pillow")
    sys.exit(1)

HERE = pathlib.Path(__file__).resolve().parent

# DXcam talks to Windows through comtypes. On newer Pythons, comtypes prints
# "Exception ignored ... ValueError: COM method call without VTable" when it cleans up
# a COM pointer that was never filled in. It is harmless, so hide just that message and
# leave every other unraisable error visible.
_orig_unraisablehook = sys.unraisablehook


def _quiet_com_release(unraisable):
    exc = unraisable.exc_value
    if isinstance(exc, ValueError) and "COM method call without VTable" in str(exc):
        return
    _orig_unraisablehook(unraisable)


sys.unraisablehook = _quiet_com_release
# mss 10.2+ renamed the class to mss.MSS (mss.mss() now warns); older mss only has mss.mss
MSS = getattr(mss, "MSS", None) or mss.mss
SS = 2                    # supersampling for smooth edges

# ============================================================== reading the game's HUD
# Measured on a 2000x1076 screenshot and a 1080p recording of the game.
SLOT = np.array([29, 27, 31], np.int16)     # colour of an HP square (semi-opaque black)
N_SQ = 20


class Geo:
    """Where the game's bars are, in client pixels. Everything follows from the HP row."""

    def __init__(self, left, right, top, bottom, H):
        self.left, self.right, self.top, self.bottom, self.H = left, right, top, bottom, H
        p = self.pitch = (right - left) / 19.82          # square + gap
        self.cx = (left + right) / 2
        self.size = 0.82 * p
        self.poise_y = top - 0.274 * p                   # thin yellow line
        self.dash_y = top - 0.745 * p                    # row of dash pills

    def pills(self, n):
        """x ranges of n dash pills: a centred row, pill 1.55 squares wide, 1.83 apart"""
        p = self.pitch
        w, step = 1.55 * p, 1.83 * p
        x0 = self.cx - (step * (n - 1) + w) / 2
        return [(x0 + i * step, x0 + i * step + w) for i in range(n)]

    def cover_box(self, n=3):
        # wide margins: the cover's soft edge must sit outside the game's bars
        p = self.pitch
        pl = self.pills(max(n, 3))
        return (min(self.left, pl[0][0]) - 1.0 * p, self.dash_y - 0.9 * p,
                max(self.right, pl[-1][1]) + 1.0 * p, self.bottom + 0.9 * p)

    @classmethod
    def default(cls, W, H):
        w = 0.605 * H
        return cls(W / 2 - w / 2, W / 2 + w / 2, H - 0.0641 * H, H - 0.0400 * H, H)

    def as_tuple(self):
        return (self.left, self.right, self.top, self.bottom)


def detect_geo(band, y0, W, H):
    """Find the row of HP squares in the bottom band. band: int16 RGB, top row at y0."""
    cx = W / 2
    x0, x1 = int(max(0, cx - 0.34 * H)), int(min(W, cx + 0.34 * H))
    m = np.abs(band[:, x0:x1] - SLOT).max(-1) <= 8
    rows = np.nonzero(m.mean(1) > 0.55)[0]
    if len(rows) < 4:
        return None
    blocks = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
    b = max(blocks, key=len)
    if not (0.012 * H <= len(b) <= 0.045 * H):
        return None
    top, bot = int(b[0]), int(b[-1]) + 1
    cols = np.nonzero(m[(top + bot) // 2])[0]
    if len(cols) < 10:
        return None
    # Only trust the LEFT end: lost HP squares turn see-through from the right, so the
    # solid row gets shorter when you're hurt. The bar is centred on the screen, so the
    # right end mirrors the left one.
    left = cols[0] + x0
    right = W - left
    if not (0.57 * H <= right - left <= 0.64 * H):
        return None
    if abs((top + y0) - (H - 0.0641 * H)) > 0.05 * H:
        return None
    g = Geo(left, right, top + y0, bot + y0, H)
    # every one of the 20 squares must really be there (not a sword or a shadow)
    yy = int((top + bot) / 2)
    hits = 0
    for i in range(N_SQ):
        xx = int(g.left + i * g.pitch + g.size / 2)
        if 0 <= xx < W and band[yy, max(0, xx - 2):xx + 3].max() < 75:
            hits += 1
    return g if hits >= 17 else None


class Reading:
    def __init__(self):
        self.visible = False
        self.hp = 1.0
        self.hp_sq = N_SQ
        self.poise = 0.0
        self.dash = [(1.0, True), (1.0, True)]
        self.lock = "none"          # none / locked
        self.stagger = False        # poise line turned red: posture broken
        self.hit = 0.0              # red damage tint at the screen edges (0..1)
        self.menu = False           # pause menu open
        self.bg = (0.5, 0.5)        # brightness behind the left / right clusters (0..1)
        self.chat_top = None        # client y where the chat text starts, if chat is up


PILL_READY = 234.0                       # a ready pill is solid cream-white


def pill_levels(bg):
    """expected brightness of (empty track, refilled part, ready pill) over background bg.
    Measured live in game across bright snow and dark ground:
        empty track    ~ 7 + 0.6*bg    (dark, partly see-through)
        refilled part  ~ 125 + 0.26*bg (light grey, growing left -> right)
        ready          ~ 234           (solid)"""
    return 7 + 0.6 * bg, 125 + 0.26 * bg, np.full_like(bg, PILL_READY)


def pill_classes(pill, bg):
    """Each column of the pill row -> 0 empty track, 1 refilled, 2 ready, or -1 when it
    matches none of them (background, or something in front of the pill)."""
    lv = pill_levels(bg)
    d = np.stack([np.abs(pill - lv[0]), np.abs(pill - lv[1]), np.abs(pill - lv[2])])
    out = d.argmin(0)
    out[d.min(0) > 16] = -1
    return out


class HudReader:
    def __init__(self, hp_full=None):
        self.geo = None
        self.geos = deque(maxlen=9)
        self.last_detect = -9.0
        self.full_cls = hp_full or "dark"
        self.ref = [70.0] * 6
        self.hist_hp = deque(maxlen=6)
        self.hp_stable = N_SQ
        self.grey_n = 0
        self.last_dash = [(1.0, True), (1.0, True)]
        self.hist_poise = deque(maxlen=3)
        self.poise_last = 0.0
        self.hist_dash = [deque(maxlen=3) for _ in range(6)]
        self.n_hist = deque(maxlen=90)
        self.n_dash = 2
        self.miss = 0
        self.lock_state, self.lock_cand, self.lock_n = "none", "none", 0
        self.stag_n = 0
        self.menu = False
        self.grey_t = -99.0
        self.respawn_t = -99.0
        self.grey_since = None
        self.miss_since = None
        self.hit_last, self.bg_last = 0.0, (0.5, 0.5)
        self.hp_cand, self.hp_cand_t, self.hp_cand_n = N_SQ, 0.0, 0
        self.lock_t = 0.0
        self.chat_y, self.chat_seen = None, -9.0

    def geometry(self, band, y0, W, H, now):
        if now - self.last_detect > 0.5 or self.geo is None:
            self.last_detect = now
            g = detect_geo(band, y0, W, H)
            if g is not None:
                self.geos.append(g.as_tuple())
            if self.geos:
                med = np.median(np.array(self.geos), 0)
                self.geo = Geo(*med, H)
            else:
                self.geo = Geo.default(W, H)
        return self.geo

    def chat_lines(self, chat, cy0, H, now):
        """Find the in-game chat (bottom left): rows full of small sharp text strokes.
        Returns the client y of the top chat line, held for a moment so the HUD doesn't
        jump around, or None when there's no chat."""
        lum = chat.mean(2)
        gx = np.abs(np.diff(lum, axis=1)) > 45
        cnt = gx.sum(1)
        text = cnt >= max(8, int(0.012 * chat.shape[1]))
        # a chat line is ~0.012H tall; ignore single noisy rows, and the "/ chat" hint row
        ys = np.nonzero(text)[0]
        found = None
        if len(ys) >= int(0.01 * H):
            blocks = np.split(ys, np.nonzero(np.diff(ys) > int(0.02 * H))[0] + 1)
            blocks = [b for b in blocks if len(b) >= int(0.008 * H)]
            if blocks:
                b = blocks[-1]                           # the block nearest the bottom
                if (b[-1] - b[0]) >= 0.02 * H:           # at least ~2 lines of chat
                    found = int(b[0] + cy0)
        if found is not None:
            self.chat_seen = now
            self.chat_y = found if self.chat_y is None else min(found, self.chat_y + 2)
        elif self.chat_y is not None and now - self.chat_seen > 1.2:
            self.chat_y = None
        return self.chat_y

    def read(self, band, y0, top, ty0, W, H, now, side=None, cols=None, chat=None):
        """band: bottom part of the client (int16 RGB, first row = client y0);
        top: strip at the top centre (first row = ty0, spans the full width);
        side: optional sample of the left middle of the screen (pause-menu check);
        cols: (x0, x1) if only that column range of band/top was captured this time
        (the rest is blank) - then the screen-edge checks keep their last values."""
        r = Reading()
        if cols is None:
            # red damage tint: the left/right edges of the bottom band turn red
            e = max(2, W // 30)
            edge = np.concatenate([band[::3, :e:3].reshape(-1, 3), band[::3, -e::3].reshape(-1, 3)])
            rb = float((edge[:, 0] - edge[:, 2]).mean())
            self.hit_last = float(np.clip((rb - 20) / 60, 0, 1))
            # brightness behind the clusters, so the ink wash can get darker on bright snow
            bl = float(band[::6, int(0.02 * W):int(0.35 * W):6].mean()) / 255
            br = float(band[::6, int(0.8 * W):int(0.98 * W):6].mean()) / 255
            self.bg_last = (bl, br)
        r.hit, r.bg = self.hit_last, self.bg_last
        if chat is not None:
            self.chat_lines(chat[0], chat[1], H, now)
        r.chat_top = self.chat_y
        valid = band if cols is None else band[:, cols[0]:cols[1]]
        # pause menu: a warm off-white panel on the left (the snow is bluish)
        if side is not None and side.size:
            m = side.reshape(-1, 3).mean(0)
            self.menu = bool(m.mean() > 195 and m[0] - m[2] > 2 and side.std() < 70)
        r.menu = self.menu
        if self.menu:                       # menu open: hide, keep the last values
            r.hp_sq, r.hp = self.hp_stable, self.hp_stable / N_SQ
            r.dash, r.visible = self.last_dash, False
            return r
        # death screen / impact frames are completely colourless: hide, keep last values
        sm = valid[::4, ::4]
        if float(((sm.max(-1) - sm.min(-1)) <= 4).mean()) >= 0.95:
            if self.grey_since is None:
                self.grey_since = now
            self.grey_t = now
            r.hp_sq, r.hp = self.hp_stable, self.hp_stable / N_SQ
            r.dash = self.last_dash
            # a quick impact flash (~0.1 s) doesn't hide it; the death screen (1 s+) does
            r.visible = now - self.grey_since < 0.3
            if not r.visible:
                self.miss_since = now - 99
            return r
        self.grey_since = None
        g = self.geometry(band, y0, W, H, now)
        hb = band.shape[0]

        def rows(y, half):
            a, b = int(round(y - y0 - half)), int(round(y - y0 + half)) + 1
            return max(0, min(hb - 1, a)), max(1, min(hb, b))

        # --- HP squares
        cls = []
        s = g.size
        ra, rb = rows(g.top + s * 0.5, s * 0.22)
        for i in range(N_SQ):
            x = g.left + i * g.pitch
            a = band[ra:rb, int(x + s * 0.28):int(x + s * 0.72)]
            if a.size == 0:
                cls.append("clear")
                continue
            px = a.reshape(-1, 3)
            m = px.mean(0)
            # a FULL square is one flat opaque colour (28,26,30); a lost square turns
            # see-through, so the world shows through it (measured live in game)
            if np.abs(m - SLOT).max() <= 5 and px.std(0).max() < 4:
                cls.append("dark")
            elif m[0] > 120 and m[0] - max(m[1], m[2]) > 60 and px.std(0).max() < 12:
                cls.append("red")
            else:
                cls.append("clear")
        solid = sum(c != "clear" for c in cls)
        # a new HP value must hold for a few reads (impact flashes and blades can fool
        # a single frame)
        # HP drains from one end, so only count lost squares that touch an end; a lone
        # odd square in the middle is something passing behind the bar
        full = [c == self.full_cls for c in cls]
        lost_l = next((i for i, f in enumerate(full) if f), N_SQ)
        lost_r = next((i for i, f in enumerate(full[::-1]) if f), N_SQ)
        val = N_SQ - min(N_SQ, max(lost_l, lost_r))
        # a new value must hold for 40 ms and 2 reads (a blade flicking past lasts less)
        if val != self.hp_cand:
            self.hp_cand, self.hp_cand_t, self.hp_cand_n = val, now, 1
        else:
            self.hp_cand_n += 1
        if self.hp_cand_n >= 2 and now - self.hp_cand_t >= 0.04:
            # just after a respawn the squares fade in, which can look like lost HP:
            # for 3 s after respawning HP may only go up
            if self.hp_stable == 0 and val > 0:
                self.respawn_t = now
            if not (val < self.hp_stable and now - self.respawn_t < 3.0):
                self.hp_stable = val
        r.hp_sq = self.hp_stable
        r.hp = r.hp_sq / N_SQ

        # --- poise: yellow line growing out EVENLY both ways from the centre. Sparks and
        # blades crossing the row are lopsided or patchy, so they are ignored.
        ra, rb = rows(g.poise_y, max(1.5, 0.12 * g.pitch))
        row = band[ra:rb, int(g.left):int(g.right)]
        half = (g.right - g.left) / 2
        # the line goes yellow (228,192,131) -> orange near full (197,101,70); when your
        # posture breaks ("Weakened") it becomes a deep red (156,19,32) across the full width
        yel = ((row[..., 0] > 130) & (row[..., 1] > 70) & (row[..., 0] - row[..., 2] > 45)).any(0)
        red = ((row[..., 0] > 105) & (row[..., 1] < 60) & (row[..., 0] - row[..., 1] > 80)).any(0)
        # (not while the whole screen is tinted red from a hit)
        self.stag_n = self.stag_n + 1 if (red.mean() > 0.85 and r.hit < 0.3) else 0
        r.stagger = self.stag_n >= 2
        p = None
        cols = np.nonzero(yel)[0]
        if len(cols) >= 2:
            cxl = g.cx - int(g.left)
            lext, rext = cxl - cols[0], cols[-1] - cxl
            ext = max(lext, rext)
            span = yel[max(0, int(cxl - ext)):int(cxl + ext) + 1]
            if ext > 0 and min(lext, rext) > 0.7 * ext and span.mean() > 0.75:
                p = ext / half
        elif not yel.any():
            p = 0.0
        if r.stagger:
            p = 1.0
        if p is None:                  # lopsided / patchy: keep the last good value
            p = self.poise_last
        self.poise_last = min(1.0, p)
        self.hist_poise.append(self.poise_last)
        r.poise = float(np.median(self.hist_poise))

        # --- dash pills: a centred row (2, 3 or more depending on your build); each one
        # fills left -> right while recharging and is bright when ready
        pa, pb = rows(g.dash_y, max(1.0, 0.07 * g.pitch))
        ua, ub = rows(g.dash_y - 0.36 * g.pitch, 1)
        da, db = rows(g.dash_y + 0.33 * g.pitch, 1)
        span = int(max(0, g.cx - 5.5 * g.pitch)), int(min(W, g.cx + 5.5 * g.pitch))
        prow = band[pa:pb, span[0]:span[1]].mean(2).mean(0)
        # background per column: the nearer-matching of the rows above and below
        up = band[ua:ub, span[0]:span[1]].mean(2).mean(0)
        dn = band[da:db, span[0]:span[1]].mean(2).mean(0)
        bgrow = np.where(np.abs(up - dn) < 25, (up + dn) / 2, np.minimum(up, dn))
        cls = pill_classes(prow, bgrow)
        # a column only counts as "pill" if it also stands out from the background
        is_pill = (cls >= 0) & (np.abs(prow - bgrow) > 12)

        def seg(xa, xb, arr=None):
            arr = cls if arr is None else arr
            a, b = int(xa) - span[0], int(xb) - span[0]
            return arr[max(0, a):max(0, b)]
        # how many pills: the count whose slots are most "pill-like" and whose gaps are not
        best, best_s = None, 0.45
        for n in range(1, 6):
            pl = g.pills(n)
            ins = np.concatenate([seg(a + 2, b - 2, is_pill) for a, b in pl])
            gaps = [(pl[i][1] + 2, pl[i + 1][0] - 2) for i in range(n - 1)]
            gaps += [(pl[0][0] - 0.6 * g.pitch, pl[0][0] - 3), (pl[-1][1] + 3, pl[-1][1] + 0.6 * g.pitch)]
            outs = np.concatenate([seg(a, b, is_pill) for a, b in gaps] + [np.zeros(0, bool)])
            if ins.size < 5:
                continue
            s = float(ins.mean()) - (float(outs.mean()) if outs.size else 0.0)
            if s > best_s + 0.05:
                best, best_s = n, s
        if best and now - self.grey_t > 4.0:     # not while respawning (pills hidden/empty)
            self.n_hist.append(best)
            vals, counts = np.unique(np.array(self.n_hist), return_counts=True)
            if counts.max() >= 20:        # change only after ~1/3 s of agreement
                self.n_dash = int(vals[counts.argmax()])
        # whole row black with nothing bright in it = the screen is fading out, not empty
        # pills (a single pill over your black armour is fine: the others are still lit)
        blackout = float(prow.max()) < 60 and float(bgrow.mean()) < 20
        dash = []
        for k, (xa, xb) in enumerate(g.pills(self.n_dash)):
            c = seg(xa + 2, xb - 2)
            known = c[c >= 0]
            if known.size < 0.5 * max(1, c.size) or blackout:   # hidden, or a black screen fade
                # mostly hidden (a sword or body in front): keep what we had
                prev = self.last_dash[k] if k < len(self.last_dash) else (1.0, True)
                dash.append(prev)
                continue
            ready = float((known == 2).mean()) > 0.8
            # refilled part grows from the left: the share of light (refilled) columns
            fill = float((known >= 1).mean())
            self.hist_dash[k].append(1.0 if ready else min(fill, 0.97))
            v = float(np.median(self.hist_dash[k]))
            dash.append((v, v >= 1.0))
        r.dash = self.last_dash = dash

        # --- lock-on. When locked, the game shows the target's name (pink OR grey-white,
        # depending on the player) with a thin dark bar under it holding the target's
        # gold poise mark. The bar only exists while locked; the "Q lock on" /
        # "looking for an opponent" prompts have none.
        if top is not None and top.size:
            t = top[:, int(W / 2 - 0.12 * H):int(W / 2 + 0.12 * H)]
            pink = ((t[..., 0] > 135) & (t[..., 0] - t[..., 1] > 50) & (t[..., 1] < 125)
                    & (t[..., 0] - t[..., 2] > 10)).sum()
            lum = top[:, int(W / 2 - 0.095 * H):int(W / 2 + 0.095 * H)].mean(2)
            bar = 0.0
            d = max(2, int(round(0.0022 * H)))           # ~3 px at 1377
            ya, yb = int(0.022 * H), min(lum.shape[0] - d - 1, int(0.05 * H))
            for yy in range(max(d, ya), yb):
                row = lum[yy]
                bar = max(bar, float(((lum[yy - d] - row > 12) & (lum[yy + d] - row > 12)).mean()))
            cand = "locked" if (pink > 0.00012 * W * H or bar > 0.6) else "none"
            if cand == self.lock_cand:
                self.lock_n += 1
            else:
                self.lock_cand, self.lock_n, self.lock_t = cand, 1, now
            if self.lock_n >= 2 and now - self.lock_t >= 0.12:
                self.lock_state = cand
        r.lock = self.lock_state

        # --- is the HUD on screen at all? (gone on the death screen and in menus)
        ok = solid >= 1 or any(f > 0.05 for f, _ in dash) or r.poise > 0
        if ok:
            self.miss_since = None
        elif self.miss_since is None:
            self.miss_since = now
        r.visible = (self.miss_since is None or now - self.miss_since < 0.6) and not self.menu
        return r


# ============================================================== animation state
def ease(cur, tgt, dt, k):
    return cur + (tgt - cur) * (1 - math.exp(-dt * k))


class Anim:
    def __init__(self):
        self.hp = self.hp_disp = self.chip = 1.0
        self.hp_sq = N_SQ
        self.chip_wait = 0.0
        self.dmg_t = self.heal_t = -9.0
        self.poise = self.poise_disp = 0.0
        self.poise_t = -9.0
        self.dash = [{"disp": 1.0, "ready": True, "pop": -9.0, "used": -9.0} for _ in range(2)]
        self.vis = 0.0
        self.idle = 1.0
        self.active_t = 0.0
        self.lock = "none"
        self.lock_t = -9.0
        self.kick_t = -9.0
        self.user_on = True
        self.stagger = False
        self.stagger_t = -9.0
        self.hit_t = -9.0
        self.hit_on = False
        self.bg = (0.5, 0.5)

    def update(self, r, now, dt, focused=True, keys=None):
        keys = keys or {}
        # red damage tint: shake + flash the crest even if no HP square was lost
        if r.hit > 0.35 and not self.hit_on:
            self.hit_on, self.hit_t = True, now
            self.dmg_t = max(self.dmg_t, now)
        elif r.hit < 0.15:
            self.hit_on = False
        if r.stagger and not self.stagger:
            self.stagger_t = now
        self.stagger = r.stagger
        self.bg = (ease(self.bg[0], r.bg[0], dt, 2), ease(self.bg[1], r.bg[1], dt, 2))
        if r.hp < self.hp - 0.01:
            self.dmg_t = now
            self.chip = max(self.chip, self.hp_disp)
            self.chip_wait = now + 0.55
        elif r.hp > self.hp + 0.01:
            self.heal_t = now
        self.hp, self.hp_sq = r.hp, r.hp_sq
        # snappy: the steel bar lands on a hit almost at once (the crimson chip is what
        # lingers), and heals sweep up quickly
        self.hp_disp = ease(self.hp_disp, self.hp, dt, 45 if self.hp < self.hp_disp else 14)
        if now > self.chip_wait:
            self.chip = ease(self.chip, self.hp_disp, dt, 3.5)
        self.chip = max(self.chip, self.hp_disp)
        if r.poise > self.poise + 0.04:
            self.poise_t = now
        self.poise = r.poise
        self.poise_disp = ease(self.poise_disp, self.poise, dt, 35)
        while len(self.dash) < len(r.dash):           # dash count can change (upgrades)
            self.dash.append({"disp": 1.0, "ready": True, "pop": now, "used": -9.0})
        del self.dash[len(r.dash):]
        for d, (fill, ready) in zip(self.dash, r.dash):
            if ready and not d["ready"]:
                d["pop"] = now
            if d["ready"] and not ready:
                d["used"] = now
                d["disp"] = 0.0 if fill < 0.2 else d["disp"]
            d["ready"] = ready
            d["disp"] = 1.0 if ready else ease(d["disp"], fill, dt, 35)
        if r.lock != self.lock:
            self.lock_t = now
        self.lock = r.lock
        if keys.get("kick"):
            self.kick_t = now
        busy = (self.hp < 0.999 or self.poise > 0.02 or not all(d["ready"] for d in self.dash)
                or now - self.dmg_t < 6 or self.lock == "locked" or now - self.kick_t < 3)
        if busy:
            self.active_t = now
        self.idle = ease(self.idle, 1.0 if now - self.active_t < 4 else 0.5, dt, 3)
        want = 1.0 if (r.visible and focused and self.user_on) else 0.0
        self.vis = ease(self.vis, want, dt, 7 if want else 9)

    def animating_left(self, now):
        """a timed effect is playing on the health / poise cluster"""
        return any([now - self.dmg_t < 0.6, now - self.heal_t < 0.7, now - self.poise_t < 0.5,
                    now - self.stagger_t < 0.9, self.stagger, self.hp_disp < 0.3,
                    self.poise_disp > 0.9, abs(self.chip - self.hp_disp) > 0.002,
                    abs(self.poise_disp - self.poise) > 0.002, abs(self.hp_disp - self.hp) > 0.002])

    def animating_right(self, now):
        """a timed effect is playing on the dash / kick / lock cluster"""
        d = [now - self.kick_t < 0.45, now - self.lock_t < 0.4]
        for x in self.dash:
            d += [now - x["pop"] < 0.45, now - x["used"] < 0.35]
        return any(d)

    def animating(self, now):
        return self.animating_left(now) or self.animating_right(now)

    def sig_left(self):
        return (self.hp_sq, round(self.hp_disp, 3), round(self.chip, 3), round(self.poise_disp, 3),
                self.stagger, round(self.bg[0], 1))

    def sig_right(self):
        return (self.lock, tuple((round(x["disp"], 3), x["ready"]) for x in self.dash),
                round(self.bg[1], 1))

    def signature(self):
        return (round(self.vis, 2), round(self.idle, 2), self.sig_left(), self.sig_right())


# ============================================================== drawing
SILVER = (228, 224, 214)
STEEL = (178, 174, 166)
DIM = (112, 108, 102)
INK = (12, 11, 14)
CRIMSON = (178, 40, 36)
GOLD = (212, 166, 82)
GOLD_HI = (255, 226, 150)

_FONTS = {}
_WASH = {}


def font(size, bold=True):
    key = (int(size), bold)
    if key not in _FONTS:
        names = (["palab.ttf", "georgiab.ttf", "timesbd.ttf", "DejaVuSerif-Bold.ttf"] if bold else
                 ["pala.ttf", "georgia.ttf", "times.ttf", "DejaVuSerif.ttf"])
        f = None
        for n in names:
            try:
                f = ImageFont.truetype(n, max(6, int(size)))
                break
            except OSError:
                continue
        _FONTS[key] = f or ImageFont.load_default()
    return _FONTS[key]


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def rgba(c, a):
    return (c[0], c[1], c[2], int(max(0, min(255, a))))


def diamond(cx, cy, R):
    return [(cx, cy - R), (cx + R, cy), (cx, cy + R), (cx - R, cy)]


def clip_below(poly, yl):
    """part of a convex polygon with y >= yl (Sutherland-Hodgman, one edge)"""
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        pin, qin = p[1] >= yl, q[1] >= yl
        if pin:
            out.append(p)
        if pin != qin:
            t = (yl - p[1]) / (q[1] - p[1])
            out.append((p[0] + (q[0] - p[0]) * t, yl))
    return out


class Canvas:
    """A layer drawn in 'base' units (1080p pixels), supersampled, with a blurred glow
    layer underneath. finish() -> premultiplied BGRA numpy array for the window."""

    def __init__(self, w, h, scale):
        # big screens need less supersampling for the same smoothness (and it's faster)
        self.ss = SS if scale < 1.25 else 1.5
        self.k = scale * self.ss
        self.size = (max(1, int(w * scale)), max(1, int(h * scale)))
        big = (int(round(self.size[0] * self.ss)), int(round(self.size[1] * self.ss)))
        self.img = Image.new("RGBA", big, (0, 0, 0, 0))
        self.glow = Image.new("RGBA", big, (0, 0, 0, 0))
        self.d, self.g = ImageDraw.Draw(self.img), ImageDraw.Draw(self.glow)
        self.wash = None
        self.ox = self.oy = 0.0
        self.has_glow = False

    def P(self, pts):
        return [((x + self.ox) * self.k, (y + self.oy) * self.k) for x, y in pts]

    def _dr(self, glow):
        if glow:
            self.has_glow = True
        return self.g if glow else self.d

    def poly(self, pts, fill=None, outline=None, width=1.0, glow=False):
        d, P = self._dr(glow), self.P(pts)
        if fill:
            d.polygon(P, fill=fill)
        if outline:
            d.line(P + [P[0]], fill=outline, width=max(1, int(round(width * self.k))),
                   joint="curve")

    def line(self, pts, fill, width=1.0, glow=False):
        self._dr(glow).line(self.P(pts), fill=fill, width=max(1, int(round(width * self.k))),
                            joint="curve")

    def arc(self, cx, cy, r, start, end, fill, width=1.0, glow=False):
        """arc of a circle; angles in degrees, 0 = 12 o'clock, clockwise"""
        if end - start <= 0.05:
            return
        (x, y), = self.P([(cx, cy)])
        rr = r * self.k
        self._dr(glow).arc([x - rr, y - rr, x + rr, y + rr], start - 90, end - 90, fill=fill,
                           width=max(1, int(round(width * self.k))))

    def ellipse(self, cx, cy, r, fill=None, outline=None, width=1.0, glow=False):
        (x, y), = self.P([(cx, cy)])
        rr = r * self.k
        self._dr(glow).ellipse([x - rr, y - rr, x + rr, y + rr], fill=fill, outline=outline,
                               width=max(1, int(round(width * self.k))))

    def text(self, x, y, s, size, fill, anchor="mm", bold=True, spacing=0.0, stroke=1.6):
        """text with a dark ink outline so it reads on bright snow too"""
        (px, py), = self.P([(x, y)])
        f = font(size * self.k, bold)
        sw = max(1, int(round(stroke * self.k))) if stroke else 0
        sf = (8, 7, 10, min(255, int(fill[3] * 0.9))) if stroke else None
        if spacing:
            # letter-spaced small caps look
            ws = [self.d.textlength(ch, font=f) for ch in s]
            total = sum(ws) + spacing * self.k * (len(s) - 1)
            cx = px - total / 2 if anchor[0] == "m" else px
            for ch, w in zip(s, ws):
                self.d.text((cx, py), ch, font=f, fill=fill, anchor="l" + anchor[1],
                            stroke_width=sw, stroke_fill=sf)
                cx += w + spacing * self.k
        else:
            self.d.text((px, py), s, font=f, fill=fill, anchor=anchor, stroke_width=sw, stroke_fill=sf)

    def ink_wash(self, pts, a):
        """a very soft dark smear behind the cluster (blurred a lot; built once and cached)"""
        a = int(round(a / 15) * 15)                 # a few steps, so the cache stays small
        key = (tuple(pts), a, self.img.size, self.k)
        if key not in _WASH:
            full = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
            ImageDraw.Draw(full).polygon([(x * self.k, y * self.k) for x, y in pts], fill=(0, 0, 0, a))
            sm = full.resize((self.img.size[0] // 4, self.img.size[1] // 4), Image.BILINEAR)
            sm = sm.filter(ImageFilter.GaussianBlur(4 * self.k))
            if len(_WASH) > 40:
                _WASH.clear()
            _WASH[key] = sm.resize(self.img.size, Image.BILINEAR)
        self.wash = _WASH[key]

    def finish(self, alpha):
        base = self.wash.copy() if self.wash is not None else \
            Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        if self.has_glow:
            sm = self.glow.reduce(4)                               # fast box shrink
            sm = sm.filter(ImageFilter.GaussianBlur(1.3 * self.k))
            base.alpha_composite(sm.resize(self.img.size, Image.BILINEAR))
        base.alpha_composite(self.img)
        pm = base.convert("RGBa")
        small = pm.reduce(int(self.ss)) if self.ss == int(self.ss) else pm.resize(self.size, Image.BOX)
        if small.size != self.size:
            small = small.resize(self.size, Image.BILINEAR)
        a = np.asarray(small)[..., [2, 1, 0, 3]]
        alpha = max(0.0, min(1.0, alpha))
        if alpha < 0.999:
            a = (a.astype(np.uint16) * int(alpha * 256) >> 8).astype(np.uint8)
        return np.ascontiguousarray(a)


# ---------------------------------------------------------------- icons (original art)
def icon_crest(c, cx, cy, R, col, accent):
    """a katana laid across the diamond with a crimson tassel"""
    ux, uy = math.cos(-math.pi / 4), math.sin(-math.pi / 4)   # up-right
    nx, ny = -uy, ux
    base = (cx - 0.34 * R * ux, cy - 0.34 * R * uy)
    tip = (cx + 0.50 * R * ux, cy + 0.50 * R * uy)
    w = 0.075 * R
    c.poly([(base[0] + nx * w, base[1] + ny * w), (tip[0] + nx * w * 0.2, tip[1] + ny * w * 0.2),
            tip, (base[0] - nx * w * 0.6, base[1] - ny * w * 0.6)], fill=rgba(col, 255))
    c.line([base, tip], rgba((255, 255, 255), 120), 0.6)
    g = (cx - 0.36 * R * ux, cy - 0.36 * R * uy)                    # guard
    c.line([(g[0] + nx * 0.16 * R, g[1] + ny * 0.16 * R), (g[0] - nx * 0.16 * R, g[1] - ny * 0.16 * R)],
           rgba(col, 255), 0.11 * R)
    h1 = (cx - 0.40 * R * ux, cy - 0.40 * R * uy)                   # handle
    h2 = (cx - 0.66 * R * ux, cy - 0.66 * R * uy)
    c.line([h1, h2], rgba(INK, 255), 0.13 * R)
    c.line([h1, h2], rgba(accent, 255), 0.07 * R)
    c.poly([(h2[0], h2[1]), (h2[0] - 0.05 * R, h2[1] + 0.18 * R), (h2[0] + 0.02 * R, h2[1] + 0.30 * R),
            (h2[0] + 0.07 * R, h2[1] + 0.16 * R)], fill=rgba(accent, 240))


def icon_dash(c, cx, cy, R, col, a):
    for off in (-0.26, 0.06):
        x = cx + off * R
        c.line([(x - 0.06 * R, cy - 0.26 * R), (x + 0.16 * R, cy), (x - 0.06 * R, cy + 0.26 * R)],
               rgba(col, a), 0.095 * R)
    for i, yy in enumerate((-0.14, 0.0, 0.14)):
        x0 = cx - (0.58 - 0.06 * abs(i - 1)) * R
        c.line([(x0, cy + yy * R), (x0 + 0.17 * R, cy + yy * R)], rgba(col, a * 0.55), 0.04 * R)


def icon_kick(c, cx, cy, R, col, a):
    pts = []
    for i in range(16):
        rr = (0.46 if i % 2 == 0 else 0.17) * R * (1.0 if i % 4 == 0 else 0.78)
        t = i * math.pi / 8 - math.pi / 2
        pts.append((cx + rr * math.cos(t), cy + rr * math.sin(t)))
    c.poly(pts, fill=rgba(col, a))


def icon_lock(c, cx, cy, R, col, a, spin=0.0):
    c.ellipse(cx, cy, 0.30 * R, outline=rgba(col, a), width=0.07 * R)
    for i in range(4):
        t = spin + i * math.pi / 2
        c.line([(cx + 0.38 * R * math.cos(t), cy + 0.38 * R * math.sin(t)),
                (cx + 0.56 * R * math.cos(t), cy + 0.56 * R * math.sin(t))], rgba(col, a), 0.07 * R)
    c.ellipse(cx, cy, 0.07 * R, fill=rgba(col, a))


def diamond_frame(c, cx, cy, R, border, fill_a=200, glow=None, inner=DIM, accents=True):
    if glow:
        c.poly(diamond(cx, cy, R + 2), outline=glow, width=5, glow=True)
    c.poly(diamond(cx, cy, R + 5), fill=(0, 0, 0, 70))                      # soft shadow
    c.poly(diamond(cx, cy, R), fill=rgba(INK, fill_a))
    c.poly(diamond(cx, cy, R), outline=border, width=1.6)
    c.poly(diamond(cx, cy, R - 6), outline=rgba(inner, 150), width=0.8)
    if accents:                                                             # ink ticks at the tips
        c.line([(cx - R - 9, cy), (cx - R - 2, cy)], border, 1.2)
        c.line([(cx + R + 2, cy), (cx + R + 9, cy)], border, 1.2)
        c.line([(cx, cy - R - 7), (cx, cy - R - 2)], border, 1.0)


# ---------------------------------------------------------------- the two clusters
LEFT_W, LEFT_H = 680, 160
RIGHT_W, RIGHT_H = 300, 215


def draw_left(an, now, scale, alpha):
    c = Canvas(LEFT_W, LEFT_H, scale)
    since = now - an.dmg_t
    if since < 0.35:                                                        # hit shake
        amp = 5 * (1 - since / 0.35) ** 2
        c.ox, c.oy = random.uniform(-amp, amp), random.uniform(-amp, amp) * 0.6
    hit = max(0.0, 1 - since / 0.5)
    low = an.hp_disp < 0.3
    pulse = 0.5 + 0.5 * math.sin(now * 6.0)
    # soft ink wash behind everything; darker when the scene behind is bright snow
    c.ink_wash([(30, 30), (520, 50), (650, 76), (520, 102), (30, 150)],
               130 + 110 * max(0.0, min(1.0, (an.bg[0] - 0.35) / 0.45)))
    stag_age = now - an.stagger_t
    stag = an.stagger or stag_age < 0.9
    # --- crest diamond
    dx, dy, R = 84, 78, 54
    border = mix(SILVER, (255, 90, 80), max(hit, 0.8 if an.stagger else 0))
    glow = rgba(CRIMSON, 200 * hit) if hit > 0.02 else (
        rgba(CRIMSON, 90 * pulse) if (low or an.stagger) else None)
    diamond_frame(c, dx, dy, R, rgba(border, 235), glow=glow)
    icon_crest(c, dx, dy, R, mix(SILVER, (255, 150, 140), hit), CRIMSON)
    # number under the crest (HP squares left), like a consumable count
    nc = mix(SILVER, (255, 110, 100), max(hit, pulse if low else 0))
    c.text(dx - 40, dy + R + 14, str(an.hp_sq), 26 + 6 * hit, rgba(nc, 250), anchor="mm", stroke=2.2)
    # --- health bar
    x0, x1, y, th = 152, 640, 70, 9.0
    c.line([(dx + R + 2, dy), (x0 - 4, dy)], rgba(STEEL, 150), 1.0)          # joins the crest
    c.line([(x0 - 4, dy), (x0 - 4, y - th / 2)], rgba(STEEL, 150), 1.0)
    span = x1 - x0 - 7
    track = [(x0, y - th / 2), (x1, y - th / 2), (x1 - 7, y + th / 2), (x0, y + th / 2)]
    c.poly(track, fill=rgba(INK, 185), outline=rgba((92, 88, 84), 160), width=0.8)
    xf = x0 + span * max(0.0, min(1.0, an.hp_disp))
    xc = x0 + span * max(0.0, min(1.0, an.chip))
    if xc > xf + 0.5:                                                       # crimson chip
        c.poly([(xf, y - th / 2 + 1), (xc, y - th / 2 + 1), (xc, y + th / 2 - 1), (xf, y + th / 2 - 1)],
               fill=rgba(mix(CRIMSON, (255, 120, 90), hit * 0.6), 235))
    if xf > x0 + 0.5:
        top_c = mix(SILVER, (238, 120, 110), pulse * 0.7) if low else SILVER
        bot_c = mix((168, 164, 156), (150, 60, 55), pulse * 0.7) if low else (168, 164, 156)
        n = 6
        for i in range(n):                                                  # vertical gradient
            yy0 = y - th / 2 + 1 + i * (th - 2) / n
            c.poly([(x0, yy0), (xf, yy0), (xf, yy0 + (th - 2) / n), (x0, yy0 + (th - 2) / n)],
                   fill=rgba(mix(top_c, bot_c, i / (n - 1)), 245))
        for i in range(1, N_SQ):                                            # the game's 20 squares
            tx = x0 + span * i / N_SQ
            if tx < xf - 1:
                c.line([(tx, y - th / 2 + 1), (tx, y + th / 2 - 1)], rgba(INK, 70), 0.7)
        c.line([(xf, y - th / 2 - 2), (xf, y + th / 2 + 2)], rgba((255, 255, 255), 255), 1.4)
    heal = max(0.0, 1 - (now - an.heal_t) / 0.6)
    if heal > 0.02:
        c.poly([(x0, y - th / 2), (xf, y - th / 2), (xf, y + th / 2), (x0, y + th / 2)],
               fill=rgba((255, 255, 255), 150 * heal), glow=True)
    if hit > 0.05:
        c.poly(track, fill=rgba(CRIMSON, 160 * hit), glow=True)
    # --- poise bar (gold), under the health bar
    px0, px1, py, pth = 152, 470, 84, 4.5
    pspan = px1 - px0 - 4
    ptrack = [(px0, py - pth / 2), (px1, py - pth / 2), (px1 - 4, py + pth / 2), (px0, py + pth / 2)]
    c.poly(ptrack, fill=rgba((34, 27, 16), 170), outline=rgba((92, 74, 40), 140), width=0.6)
    pv = max(0.0, min(1.0, an.poise_disp))
    flare = max(0.0, 1 - (now - an.poise_t) / 0.45)
    brk = pv > 0.9
    if stag:
        # POSTURE BROKEN: the gold bar shatters into crimson shards, then burns crimson
        # while you are staggered
        u = min(1.0, stag_age / 0.9)
        if an.stagger:
            hot = 0.55 + 0.45 * math.sin(now * 14)
            c.poly([(px0, py - pth / 2 - 0.5), (px1, py - pth / 2 - 0.5), (px1 - 4, py + pth / 2 + 0.5),
                    (px0, py + pth / 2 + 0.5)], fill=rgba(mix(CRIMSON, (255, 110, 90), hot), 250))
            c.poly([(px0, py - 4), (px1, py - 4), (px1, py + 4), (px0, py + 4)],
                   fill=rgba(CRIMSON, 200 * hot), glow=True)
        if u < 1.0:
            rnd = random.Random(int(an.stagger_t * 1000))
            for i in range(16):
                sx = px0 + (px1 - px0) * (i + rnd.random()) / 16
                vx, vy = rnd.uniform(-60, 60), rnd.uniform(-70, 25)
                x, y2 = sx + vx * u, py + vy * u + 60 * u * u
                sz = rnd.uniform(2.5, 6) * (1 - u * 0.6)
                col = GOLD_HI if i % 3 == 0 else (CRIMSON if i % 3 == 1 else GOLD)
                c.poly([(x, y2 - sz), (x + sz * 0.6, y2), (x, y2 + sz * 0.5), (x - sz * 0.7, y2 - sz * 0.1)],
                       fill=rgba(col, 255 * (1 - u)))
            c.poly(diamond(dx, dy, R + 10 + 30 * u), outline=rgba(CRIMSON, 220 * (1 - u)), width=2.5)
    elif pv > 0.003:
        xp = px0 + pspan * pv
        # like the game's line: gold, heating toward ember-orange as poise runs out
        base_c = mix(GOLD, (230, 112, 58), max(0.0, (pv - 0.5) / 0.5))
        col = mix(base_c, GOLD_HI, max(flare, pulse if brk else 0))
        c.poly([(px0, py - pth / 2 + 0.6), (xp, py - pth / 2 + 0.6), (xp, py + pth / 2 - 0.6),
                (px0, py + pth / 2 - 0.6)], fill=rgba(col, 245))
        c.line([(xp, py - pth / 2 - 1.5), (xp, py + pth / 2 + 1.5)], rgba(GOLD_HI, 255), 1.1)
        if flare > 0.02 or brk:
            g = flare if not brk else 0.5 + 0.5 * pulse
            c.poly([(px0, py - 3), (xp, py - 3), (xp, py + 3), (px0, py + 3)],
                   fill=rgba(GOLD, 170 * g), glow=True)
    # faint ink underline that ties the group together
    for i in range(10):
        a = 60 * (1 - i / 10)
        c.line([(152 + i * 50, 97), (152 + (i + 1) * 50, 97)], rgba(STEEL, a), 0.6)
    return c.finish(alpha)


def right_width(n):
    return RIGHT_W


def draw_dash_button(c, an, now, cx, cy, R):
    """One dash button: a diamond (the HUD's frame) holding a steel ring split into one
    segment per dash charge - a full ring = every dash ready, 1 of 2 = half the ring,
    each of 3 = a third. Each segment fills clockwise as its charge refills, flashes
    white when it comes back, and flares crimson when spent."""
    n = max(1, len(an.dash))
    ready_n = sum(d["ready"] for d in an.dash)
    pops = [max(0.0, 1 - (now - d["pop"]) / 0.45) for d in an.dash]
    useds = [max(0.0, 1 - (now - d["used"]) / 0.35) for d in an.dash]
    sc = 1 + 0.08 * max(pops) ** 2 - 0.05 * max(useds)
    Rs = R * sc
    glow = None
    if max(pops) > 0.02:
        glow = rgba((255, 255, 255), 200 * max(pops))
    elif max(useds) > 0.02:
        glow = rgba(CRIMSON, 190 * max(useds))
    border = rgba(mix(DIM, SILVER, 1.0 if ready_n else 0.4), 235)
    diamond_frame(c, cx, cy, Rs, border, glow=glow)
    # the ring
    rr, rw = 0.53 * Rs, 0.12 * Rs
    gap = 0.0 if n == 1 else 9.0                       # degrees between segments
    span = 360.0 / n
    c.ellipse(cx, cy, rr + rw / 2 + 1.5, outline=rgba(INK, 200), width=1.2)
    c.ellipse(cx, cy, rr - rw / 2 - 1.5, outline=rgba(INK, 200), width=1.2)
    for i, d in enumerate(an.dash):
        a0 = i * span + gap / 2
        a1 = (i + 1) * span - gap / 2
        c.arc(cx, cy, rr, a0, a1, rgba((40, 38, 42), 230), rw)            # empty groove
        f = 1.0 if d["ready"] else max(0.0, min(1.0, d["disp"]))
        ae = a0 + (a1 - a0) * f
        if d["ready"]:
            col = mix(SILVER, (255, 255, 255), pops[i])
            c.arc(cx, cy, rr, a0, a1, rgba(col, 255), rw)
            c.arc(cx, cy, rr + rw * 0.28, a0, a1, rgba((255, 255, 255), 90), rw * 0.18)  # steel sheen
            if pops[i] > 0.02:
                c.arc(cx, cy, rr, a0, a1, rgba((255, 255, 255), 230 * pops[i]), rw * 1.8, glow=True)
        elif f > 0.003:
            c.arc(cx, cy, rr, a0, ae, rgba(mix(DIM, STEEL, 0.6), 235), rw)
            # bright leading edge where the charge is filling
            t = math.radians(ae - 90)
            c.line([(cx + (rr - rw / 2) * math.cos(t), cy + (rr - rw / 2) * math.sin(t)),
                    (cx + (rr + rw / 2) * math.cos(t), cy + (rr + rw / 2) * math.sin(t))],
                   rgba((255, 255, 255), 235), 1.4)
        if useds[i] > 0.02:
            c.arc(cx, cy, rr, a0, a1, rgba(CRIMSON, 230 * useds[i]), rw * 1.2)
            c.arc(cx, cy, rr, a0, a1, rgba(CRIMSON, 200 * useds[i]), rw * 2, glow=True)
    # gold pins marking each segment boundary, like the guard notches on a tsuba
    if n > 1:
        for i in range(n):
            t = math.radians(i * span - 90)
            px, py = cx + (rr + rw / 2 + 3) * math.cos(t), cy + (rr + rw / 2 + 3) * math.sin(t)
            c.poly(diamond(px, py, 2.6), fill=rgba(GOLD, 235))
    icon_dash(c, cx, cy, Rs * 0.78, SILVER if ready_n else DIM, 255 if ready_n else 150)
    # how many dashes are ready, like the count beside a consumable
    c.text(cx - Rs * 0.78, cy + Rs * 0.62, str(ready_n), 20, rgba(SILVER if ready_n else (200, 90, 80), 250),
           anchor="mm", stroke=2.0)
    c.text(cx, cy + Rs + 15, "SHIFT", 14, rgba(SILVER, 245), spacing=2.0, stroke=2.0)


def draw_right(an, now, scale, alpha):
    w = right_width(len(an.dash))
    c = Canvas(w, RIGHT_H, scale)
    c.ink_wash([(45, 150), (110, 55), (w - 35, 70), (w - 45, 190), (55, 195)],
               110 + 80 * max(0.0, min(1.0, (an.bg[1] - 0.35) / 0.45)))
    # one big dash button (bottom left of the cluster), F (kick) and Q (lock) staggered
    draw_dash_button(c, an, now, 92, 128, 60)
    slots = [("kick", None, 190, 70, 32), ("lock", None, 252, 140, 32)]
    for kind, idx, cx, cy, R in slots:
        border, glow, icon_a, label = rgba(SILVER, 230), None, 255, ""
        sc = 1.0
        if kind == "kick":
            f = max(0.0, 1 - (now - an.kick_t) / 0.4)
            sc = 1 + 0.12 * f ** 2
            diamond_frame(c, cx, cy, R * sc, rgba(mix(SILVER, (255, 255, 255), f), 220),
                          glow=rgba((255, 240, 220), 200 * f) if f > 0.02 else None, accents=False)
            icon_kick(c, cx, cy, R * sc, mix(STEEL, (255, 255, 255), f), 200 + 55 * f)
            label = "F"
        else:
            locked = an.lock == "locked"
            t = now - an.lock_t
            snap = max(0.0, 1 - t / 0.35) if locked else 0.0
            release = max(0.0, 1 - t / 0.3) if not locked else 0.0
            col = CRIMSON if locked else STEEL
            bcol = mix(CRIMSON, (255, 120, 110), snap) if locked else mix(STEEL, CRIMSON, release)
            glow = rgba(CRIMSON, 120 + 100 * snap) if locked else None
            diamond_frame(c, cx, cy, R * (1 + 0.14 * snap ** 2), rgba(bcol, 230), glow=glow, accents=False)
            icon_lock(c, cx, cy, R, mix(col, (255, 140, 130), snap), 255 if locked else 170,
                      spin=snap * 1.2 - release * 0.8)
            label = "Q"
        c.text(cx, cy + R * sc + 15, label, 14, rgba(SILVER, 245), spacing=2.0, stroke=2.0)
    return c.finish(alpha)


def draw_cover(region, alpha):
    """Hide the game's own bars: rebuild the background there from the rows just above and
    below (a cheap in-paint), blurred, with feathered edges. region: uint8 RGB from the
    capture, which does NOT include this overlay."""
    h, w = region.shape[:2]
    if h < 6 or w < 6:
        return None
    m = max(2, h // 14)
    # Coons patch: blend from all four sides, so every edge matches what's around it
    # exactly (no visible box on bright snow). Built at 1/4 size, then smoothly enlarged.
    q = 4
    hs, ws = max(2, h // q), max(2, w // q)
    # medians, not means: a glowing blade crossing an edge row would otherwise smear its
    # colour across the whole cover
    T = np.median(region[:m, ::q], 0)[:ws].astype(np.float32)        # [ws, 3]
    B = np.median(region[-m:, ::q], 0)[:ws].astype(np.float32)
    Lc = np.median(region[::q, :m], 1)[:hs].astype(np.float32)       # [hs, 3]
    Rc = np.median(region[::q, -m:], 1)[:hs].astype(np.float32)
    ws, hs = len(T), len(Lc)
    t = np.linspace(0, 1, hs, dtype=np.float32)[:, None, None]
    u = np.linspace(0, 1, ws, dtype=np.float32)[None, :, None]
    fill = (T[None] * (1 - t) + B[None] * t + Lc[:, None] * (1 - u) + Rc[:, None] * u
            - ((1 - t) * (1 - u) * T[0] + (1 - t) * u * T[-1] + t * (1 - u) * B[0] + t * u * B[-1]))
    img = Image.fromarray(fill.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
    rgb = np.asarray(img.resize((w, h), Image.BILINEAR))[..., ::-1]          # BGR
    a = _cover_mask(h, w)
    if alpha < 0.999:
        a = (a.astype(np.uint16) * int(alpha * 256) >> 8).astype(np.uint8)
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = (rgb.astype(np.uint16) * a[..., None] >> 8).astype(np.uint8)
    out[..., 3] = a
    return out


_MASKS = {}


def _cover_mask(h, w):
    """feathered (smoothstep) edge for the cover, cached per size"""
    if (h, w) not in _MASKS:
        fx = np.minimum(np.arange(w), np.arange(w)[::-1]).astype(np.float32)
        fy = np.minimum(np.arange(h), np.arange(h)[::-1]).astype(np.float32)
        d = np.minimum(1, np.minimum(fx[None, :], fy[:, None]) / max(3.0, h * 0.22))
        if len(_MASKS) > 6:
            _MASKS.clear()
        _MASKS[(h, w)] = (255 * d * d * (3 - 2 * d)).astype(np.uint8)
    return _MASKS[(h, w)]


class HudCore:
    """reader + animation + drawing; returns layers in client coordinates"""

    def __init__(self, hp_full=None, fade=True, ui_scale=1.15):
        self.reader = HudReader(hp_full)
        self.an = Anim()
        self.fade = fade
        self.ui_scale = ui_scale
        self.cached = None
        self.chat_dodge = True
        self.left_dy = 0.0
        self.last = None
        self.reading = None

    def step(self, band, y0, top, ty0, W, H, now, focused=True, keys=None, override=None, side=None):
        dt = 1 / 60 if self.last is None else max(1e-3, min(0.1, now - self.last))
        self.last = now
        r = override if override is not None else self.reader.read(band, y0, top, ty0, W, H, now, side)
        self.reading = r
        an = self.an
        an.update(r, now, dt, focused, keys)
        layers = {}
        if an.vis < 0.01:
            return layers
        s = H / 1080.0 * self.ui_scale
        alpha = an.vis * (an.idle if self.fade else 1.0)
        # the steel/gold clusters animate at up to 60 fps, and are only redrawn when
        # something changed or an effect is playing; the cover follows every frame
        # each cluster is cached on its own and redrawn only when ITS values changed or one
        # of ITS effects is playing (at most once per 60 Hz frame)
        if self.cached is None:
            self.cached = {}
        common = (round(alpha, 2), W, H)
        for name, sig, anim, draw, pos in (
                ("left", an.sig_left(), an.animating_left(now), draw_left,
                 lambda: (int(30 * s), int(H - 200 * s))),
                # high enough to clear the game's timer / level / currency text
                ("right", an.sig_right(), an.animating_right(now), draw_right,
                 lambda: (int(W - (right_width(len(an.dash)) + 24) * s), int(H - (RIGHT_H + 150) * s)))):
            c = self.cached.get(name)
            key = (sig, common)
            if c is None or c[1] != key or (anim and now - c[0] >= 1 / 61):
                c = (now, key, (draw(an, now, s, alpha),) + pos())
                self.cached[name] = c
            layers[name] = c[2]
        # slide the health cluster up above the game chat while chat lines are showing,
        # and back down when they fade (moving the window needs no redraw)
        base_y = int(H - 200 * s)
        target = 0.0
        ct = getattr(r, "chat_top", None)
        if self.chat_dodge and ct is not None:
            target = min(0.0, (ct - 8) - (base_y + 168 * s))
        self.left_dy = ease(self.left_dy, target, dt, 12)
        img, lx, ly = layers["left"]
        layers["left"] = (img, lx, int(base_y + self.left_dy))
        g = self.reader.geo or Geo.default(W, H)
        bx0, by0, bx1, by1 = [int(round(v)) for v in g.cover_box(len(an.dash))]
        bx0, bx1 = max(0, bx0), min(W, bx1)
        ra, rb = by0 - y0, by1 - y0
        if band is not None and 0 <= ra < rb <= band.shape[0]:
            cov = draw_cover(band[ra:rb, bx0:bx1].astype(np.uint8), an.vis)
            if cov is not None:
                layers["cover"] = (cov, bx0, by0)
        return layers


# ============================================================== Windows overlay windows
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    H_ = wintypes.HANDLE
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                           ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                           ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                                           ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, H_]
    gdi32.SelectObject.restype = H_
    gdi32.DeleteObject.argtypes = [H_]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    _registered = [False]
    CLASS = "BladeHudLayer"
    EX = 0x80000 | 0x20 | 0x8 | 0x80 | 0x08000000   # layered, click-through, topmost, tool, no-activate

    class LayeredWindow:
        def __init__(self, title):
            hinst = kernel32.GetModuleHandleW(None)
            if not _registered[0]:
                wc = WNDCLASSEXW()
                wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
                wc.lpfnWndProc = _proc
                wc.hInstance = hinst
                wc.lpszClassName = CLASS
                user32.RegisterClassExW(ctypes.byref(wc))
                _registered[0] = True
            # Windows refuses capture-exclusion on a window that is already layered, so
            # create it plain, exclude it from capture, THEN make it layered.
            self.hwnd = user32.CreateWindowExW(EX & ~0x80000, CLASS, title, 0x80000000, 0, 0, 1, 1,
                                               None, None, hinst, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowEx failed ({ctypes.get_last_error()})")
            self.hidden_from_capture = bool(user32.SetWindowDisplayAffinity(self.hwnd, 0x11))
            user32.SetWindowLongPtrW(self.hwnd, -20, user32.GetWindowLongPtrW(self.hwnd, -20) | 0x80000)
            self.screen = user32.GetDC(None)
            self.mem = gdi32.CreateCompatibleDC(self.screen)
            self.bmp = None
            self.bits = ctypes.c_void_p()
            self.size = (0, 0)
            self.shown = False

        def update(self, bgra, x, y):
            h, w = bgra.shape[:2]
            if (w, h) != self.size:
                bi = BITMAPINFOHEADER()
                bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
                bits = ctypes.c_void_p()
                bmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
                if not bmp:
                    return
                gdi32.SelectObject(self.mem, bmp)
                if self.bmp:
                    gdi32.DeleteObject(self.bmp)
                self.bmp, self.bits, self.size = bmp, bits, (w, h)
            ctypes.memmove(self.bits, bgra.ctypes.data, w * h * 4)
            bf = BLENDFUNCTION(0, 0, 255, 1)
            user32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(wintypes.POINT(x, y)),
                                       ctypes.byref(wintypes.SIZE(w, h)), self.mem,
                                       ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(bf), 2)
            if not self.shown:
                user32.ShowWindow(self.hwnd, 4)       # show, don't take focus
                self.shown = True

        def hide(self):
            if self.shown:
                user32.ShowWindow(self.hwnd, 0)
                self.shown = False

        def raise_top(self):
            user32.SetWindowPos(self.hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)

        def close(self):
            user32.DestroyWindow(self.hwnd)

    def pump():
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def key_down(vk):
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def roblox_client():
        """(hwnd, x, y, w, h) of the Roblox game area on screen, or None"""
        h = user32.FindWindowW(None, "Roblox")
        if not h or user32.IsIconic(h):
            return None
        rc = wintypes.RECT()
        user32.GetClientRect(h, ctypes.byref(rc))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(h, ctypes.byref(pt))
        w, hh = rc.right - rc.left, rc.bottom - rc.top
        if w < 200 or hh < 200:
            return None
        return h, pt.x, pt.y, w, hh


# ============================================================== modes
BAND_TOP = 0.86       # only the bottom 14% of the screen holds the game's bars


def _grab(sct, x, y, w, h):
    s = sct.grab({"left": x, "top": y, "width": max(4, w), "height": max(4, h)})
    return np.frombuffer(s.bgra, np.uint8).reshape(s.height, s.width, 4)[..., 2::-1].astype(np.int16)


def grab_parts(sct, x, y, W, H, side=False):
    """Small captures only: the bar band, the lock-on name row, and (sometimes) a patch
    of the left side for the pause-menu check. The top strip is returned at full client
    width coordinates (padded) so the reader can index it from the centre."""
    y0 = int(H * BAND_TOP)
    band = _grab(sct, x, y + y0, W, H - y0)
    ty0 = int(H * 0.02)
    tw = int(0.26 * H)
    tx0 = int(W / 2 - tw / 2)
    tpart = _grab(sct, x + tx0, y + ty0, tw, int(H * 0.06))
    top = np.zeros((tpart.shape[0], W, 3), np.int16)
    top[:, tx0:tx0 + tpart.shape[1]] = tpart
    sd = _grab(sct, x + int(0.02 * W), y + int(0.25 * H), int(0.26 * W), int(0.5 * H))[::4, ::4] if side else None
    return band, y0, top, ty0, sd


def load_dxcam(path=None):
    """Import DXcam: installed, or from a DXcam source folder (e.g. the DXcam-0.3.0
    download next to this folder). Returns the module, or a string saying why not."""
    try:
        import dxcam
        return dxcam
    except ImportError:
        pass
    cands = [pathlib.Path(path)] if path else []
    for base in (HERE.parent, HERE):
        cands += sorted(base.glob("DXcam*")) + sorted(base.glob("DXcam*/DXcam*"))
    for c in cands:
        if (c / "dxcam" / "__init__.py").exists():
            sys.path.insert(0, str(c))
            try:
                import dxcam
                return dxcam
            except ImportError as e:
                sys.path.remove(str(c))
                if "comtypes" in str(e):
                    return (f"DXcam found at {c} but it needs comtypes:  "
                            "python -m pip install comtypes")
                return f"DXcam at {c} failed to load ({e})"
    return "DXcam not found (put the DXcam folder next to this one, or pip install dxcam)"


class Grabber:
    """Screen capture. Uses DXGI desktop duplication (the `dxcam` package) when it is
    installed - about 1-3 ms a grab - otherwise mss, where every grab costs a fixed
    ~11 ms whatever its size (so we make as FEW grabs as possible, not small ones).
    Both skip the HUD's own windows (they are excluded from capture)."""

    why_not = ""

    def __init__(self, dxcam_path=None):
        self.cam = None
        self.sct = MSS()
        self.kind = "mss"
        dx = load_dxcam(dxcam_path)
        if isinstance(dx, str):
            Grabber.why_not = dx
        else:
            try:
                # raw BGRA: DXcam then needs no OpenCV, we flip the channels ourselves
                self.cam = dx.create(output_color="BGRA")
                self.kind = "dxcam"
                self._check_colours()
            except Exception as e:
                Grabber.why_not = f"DXcam failed to start ({e})"
                self.cam = None
                self.kind = "mss"

    def _check_colours(self):
        """All the colours the HUD recognises were measured with normal (GDI) capture.
        With HDR switched on in Windows, DXGI duplication returns different brightness
        (about 1.7x here) and bright UI clips to white, so the readings go wrong. Compare
        the two on the same screen; if they disagree, use mss."""
        mon = self.sct.monitors[1]
        reg = (mon["left"], mon["top"], mon["width"], mon["height"])
        ratios = []
        for _ in range(3):
            d = self.cam.grab(region=(0, 0, reg[2], reg[3]), new_frame_only=False)
            s = self.sct.grab({"left": reg[0], "top": reg[1], "width": reg[2], "height": reg[3]})
            if d is None:
                continue
            m = np.frombuffer(s.bgra, np.uint8).reshape(s.height, s.width, 4)[::8, ::8, :3].astype(float)
            dd = d[::8, ::8, :3].astype(float)
            ratios.append((dd.mean() + 1) / (m.mean() + 1))
            time.sleep(0.02)
        if not ratios or abs(float(np.median(ratios)) - 1.0) > 0.06:
            r = float(np.median(ratios)) if ratios else float("nan")
            Grabber.why_not = (f"DXcam's colours differ from normal capture ({r:.2f}x - is HDR "
                               "on in Windows display settings?), so the calibrated mss capture "
                               "is used instead")
            try:
                self.cam.release()
            except Exception:
                pass
            self.cam = None
            self.kind = "mss"

    def close(self):
        cam, self.cam = self.cam, None
        if cam is not None:
            for fn in ("stop", "release"):
                try:
                    getattr(cam, fn)()
                except Exception:
                    pass
        try:
            self.sct.close()
        except Exception:
            pass

    def grab(self, x, y, w, h):
        w, h = max(4, int(w)), max(4, int(h))
        if self.cam is not None:
            try:
                # coordinates are on the primary monitor (where Roblox runs)
                f = self.cam.grab(region=(int(x), int(y), int(x) + w, int(y) + h),
                                  new_frame_only=False)
                if f is not None:
                    return f[..., 2::-1].astype(np.int16)
            except Exception as e:
                Grabber.why_not = f"DXcam stopped ({e})"
                self.cam = None
                self.kind = "mss"
        s = self.sct.grab({"left": int(x), "top": int(y), "width": w, "height": h})
        return np.frombuffer(s.bgra, np.uint8).reshape(s.height, s.width, 4)[..., 2::-1].astype(np.int16)


class Feeder(threading.Thread):
    """Capture + reading on their own thread, as fast as the capture allows, so the HUD
    always draws from the freshest reading. Most reads grab ONE tall centre column (the
    bars, dash pills and lock-on name are all in it). Every few reads it grabs the whole
    game instead, to update the red hit tint, the brightness behind the clusters and the
    pause-menu check."""

    FULL_EVERY = 5

    def __init__(self, reader, max_hz=144, dxcam_path=None):
        super().__init__(daemon=True)
        self.reader = reader
        self.dxcam_path = dxcam_path
        self.ready = threading.Event()
        self.period = 1.0 / max_hz
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.latest = None          # (reading, band, y0, rc, time)
        self.rc = None
        self.reads = 0
        self.rate = 0.0
        self.kind = "?"
        self.error = None

    def run(self):
        g = None
        try:
            g = Grabber(self.dxcam_path)
            self.kind = g.kind
            self.ready.set()
            n, t_rate, c_rate = 0, time.perf_counter(), 0
            while not self.halt.is_set():
                t0 = time.perf_counter()
                rc = roblox_client()
                self.rc = rc
                if rc is None:
                    self.halt.wait(0.3)
                    continue
                if user32.GetForegroundWindow() != rc[0]:
                    # another window is in front of the game: we'd be reading THAT window.
                    # Keep the last reading until Roblox is back in front.
                    self.halt.wait(0.05)
                    continue
                _, x, y, W, H = rc
                y0, ty0 = int(H * BAND_TOP), int(H * 0.02)
                full = n % self.FULL_EVERY == 0
                n += 1
                if g.kind == "dxcam":
                    # DXGI: one desktop frame per read, cut into small pieces - cheap
                    band = g.grab(x, y + y0, W, H - y0)
                    tw = int(0.26 * H)
                    tx0 = int(W / 2 - tw / 2)
                    tpart = g.grab(x + tx0, y + ty0, tw, int(H * 0.06))
                    top = np.zeros((tpart.shape[0], W, 3), np.int16)
                    top[:, tx0:tx0 + tpart.shape[1]] = tpart
                    side = (g.grab(x + int(0.02 * W), y + int(0.25 * H), int(0.26 * W), int(0.5 * H))[::4, ::4]
                            if full else None)
                    chat = ((g.grab(x + int(0.004 * W), y + int(0.62 * H), int(0.296 * W), int(0.345 * H)),
                             int(0.62 * H)) if full else None)
                    cols = None
                elif full:
                    img = g.grab(x, y, W, H)
                    band = img[y0:]
                    top = img[ty0:ty0 + max(4, int(H * 0.06))]
                    side = img[int(0.25 * H):int(0.75 * H):4, int(0.02 * W):int(0.28 * W):4]
                    chat = (img[int(0.62 * H):int(0.965 * H), int(0.004 * W):int(0.30 * W)], int(0.62 * H))
                    cols = None
                else:
                    cx0 = int(max(0, W / 2 - 0.37 * H))
                    cx1 = int(min(W, W / 2 + 0.37 * H))
                    col = g.grab(x + cx0, y, cx1 - cx0, H)
                    band = np.zeros((H - y0, W, 3), np.int16)
                    band[:, cx0:cx0 + col.shape[1]] = col[y0:]
                    th = max(4, int(H * 0.06))
                    top = np.zeros((th, W, 3), np.int16)
                    top[:, cx0:cx0 + col.shape[1]] = col[ty0:ty0 + th]
                    side, chat, cols = None, None, (cx0, cx0 + col.shape[1])
                now = time.perf_counter()
                r = self.reader.read(band, y0, top, ty0, W, H, now, side, cols=cols, chat=chat)
                with self.lock:
                    self.latest = (r, band, y0, rc, now)
                self.reads += 1
                c_rate += 1
                if now - t_rate >= 1.0:
                    self.rate, c_rate, t_rate = c_rate / (now - t_rate), 0, now
                d = self.period - (time.perf_counter() - t0)
                if d > 0:
                    self.halt.wait(d)
        except Exception as e:
            self.error = e
            self.ready.set()
        finally:
            if g is not None:
                g.close()

    def take(self):
        with self.lock:
            return self.latest

    def stop(self):
        self.halt.set()


def run_live(args):
    if sys.platform != "win32":
        print("The live overlay needs Windows. Try --preview or --video instead.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    wins = {k: LayeredWindow(f"BladeHUD-{k}") for k in ("cover", "left", "right")}
    if not all(w.hidden_from_capture for w in wins.values()):
        print("Warning: this Windows version can't hide the HUD from screen capture, so it "
              "can't read the bars under it. Update to Windows 10 2004+ (or run --no-cover).")
    core = HudCore(args.hp_full, fade=not args.no_fade)
    core.chat_dodge = not args.no_chat_dodge
    feeder = Feeder(core.reader, dxcam_path=args.dxcam_path)
    feeder.start()
    feeder.ready.wait(5)
    if feeder.kind == "dxcam":
        print("Capture: DXcam (fast DXGI desktop duplication)")
    else:
        print(f"Capture: mss (slower, ~11 ms a grab). {Grabber.why_not}")
    print("BLADE HUD running. Switch to Roblox (windowed / borderless).  F6 show/hide, "
          "Ctrl+F6 quit, Ctrl+C here quits.")
    f6_was, last_top, waiting, last_stat = False, 0.0, False, time.perf_counter()
    period = 1.0 / args.fps
    try:
        while True:
            t0 = time.perf_counter()
            pump()
            f6 = key_down(0x75)
            if f6 and not f6_was:
                if key_down(0x11):
                    break
                core.an.user_on = not core.an.user_on
            f6_was = f6
            if feeder.error:
                print(f"Capture stopped: {feeder.error}")
                break
            got = feeder.take()
            rc = feeder.rc
            if rc is None or got is None:
                if not waiting and rc is None:
                    print("Waiting for the Roblox window...")
                    waiting = True
                for w in wins.values():
                    w.hide()
                time.sleep(0.1)
                continue
            waiting = False
            r, band, y0, grc, _ = got
            hwnd, x, y, W, H = grc
            fg = user32.GetForegroundWindow()
            focused = fg == hwnd or fg in [w.hwnd for w in wins.values()]
            keys = {"kick": focused and key_down(0x46)}
            layers = core.step(band, y0, None, 0, W, H, time.perf_counter(), focused, keys,
                               override=r)
            if args.stats and time.perf_counter() - last_stat > 2:
                last_stat = time.perf_counter()
                print(f"  capture: {feeder.kind}, {feeder.rate:.0f} reads/s", flush=True)
            for name, w in wins.items():
                if name == "cover" and args.no_cover:
                    w.hide()
                    continue
                if name in layers:
                    img, lx, ly = layers[name]
                    w.update(img, x + lx, y + ly)
                else:
                    w.hide()
            if time.perf_counter() - last_top > 1.0:
                last_top = time.perf_counter()
                for w in wins.values():
                    w.raise_top()
            d = period - (time.perf_counter() - t0)
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        feeder.stop()
        feeder.join(1.0)
        for w in wins.values():
            w.close()
        print("BLADE HUD closed.")


def unpremult(bgra):
    a = bgra[..., 3:4].astype(np.float32)
    rgb = bgra[..., [2, 1, 0]].astype(np.float32) * 255 / np.maximum(a, 1)
    return Image.fromarray(np.concatenate([rgb.clip(0, 255), a], -1).astype(np.uint8), "RGBA")


def compose(frame_rgb, core, now, override=None, keys=None):
    H, W = frame_rgb.shape[:2]
    y0, ty0 = int(H * BAND_TOP), int(H * 0.02)
    band = frame_rgb[y0:].astype(np.int16)
    top = frame_rgb[ty0:ty0 + max(4, int(H * 0.06))].astype(np.int16)
    side = frame_rgb[int(0.25 * H):int(0.75 * H):4, int(0.02 * W):int(0.28 * W):4].astype(np.int16)
    layers = core.step(band, y0, top, ty0, W, H, now, True, keys, override, side=side)
    img = Image.fromarray(frame_rgb).convert("RGBA")
    for name in ("cover", "left", "right"):
        if name in layers:
            arr, lx, ly = layers[name]
            img.alpha_composite(unpremult(arr), (max(0, lx), max(0, ly)))
    return img.convert("RGB")


def run_preview(path, out=None):
    fr = np.asarray(Image.open(path).convert("RGB"))
    core = HudCore()
    img = None
    for i in range(45):                     # let it settle (fade-in, smoothing)
        img = compose(fr, core, i / 60)
    out = out or HERE / "hud_preview.png"
    img.save(out)
    r = core.reading
    print(f"read: HP {r.hp_sq}/{N_SQ}  poise {r.poise:.2f}  dash "
          f"{[round(f, 2) for f, _ in r.dash]}  lock {r.lock}  visible {r.visible}")
    print(f"Saved {out}")


def demo_reading(t):
    """scripted values that walk through every animation"""
    r = Reading()
    r.visible = True
    cyc = t % 16
    r.hp_sq = N_SQ if cyc < 3 else (14 if cyc < 6 else (5 if cyc < 10 else (12 if cyc < 13 else N_SQ)))
    r.hp = r.hp_sq / N_SQ
    r.poise = max(0.0, min(1.0, (cyc - 2) / 5)) if cyc < 8 else max(0.0, 1 - (cyc - 8) / 2)
    d1 = 1.0 if (cyc % 5) < 1 else min(1.0, ((cyc % 5) - 1) / 2.5)
    d2 = 1.0 if (cyc % 7) < 2 else min(1.0, ((cyc % 7) - 2) / 2.5)
    d3 = 1.0 if (cyc % 9) < 3 else min(1.0, ((cyc % 9) - 3) / 2.5)
    r.dash = [(d1, d1 >= 1.0), (d2, d2 >= 1.0), (d3, d3 >= 1.0)]
    r.lock = "locked" if 4 < cyc < 11 else "none"
    r.stagger = 7.0 < cyc < 8.2
    r.hit = 1.0 if 3.0 < cyc < 3.2 else 0.0
    r.bg = (0.8, 0.8) if cyc > 12 else (0.3, 0.3)
    return r


def run_demo(args):
    """fake values that walk through every animation, live on your main screen"""
    if sys.platform != "win32":
        print("Live demo needs Windows.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    wins = {k: LayeredWindow(f"BladeHUD-{k}") for k in ("left", "right")}
    core = HudCore(fade=False)
    with MSS() as sct:
        m = sct.monitors[1]
    W, H, x, y = m["width"], m["height"], m["left"], m["top"]
    print("Demo for 32 s: watch the bottom corners of your main screen. Ctrl+C to stop.")
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < 32:
            pump()
            now = time.perf_counter()
            keys = {"kick": int((now - t0) * 2) % 9 == 0}
            layers = core.step(None, 0, None, 0, W, H, now, True, keys, demo_reading(now - t0))
            for name, w in wins.items():
                if name in layers:
                    img, lx, ly = layers[name]
                    w.update(img, x + lx, y + ly)
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        pass
    finally:
        for w in wins.values():
            w.close()


def run_video(path, out, step=2, sheet_every=0):
    try:
        import cv2
    except ImportError:
        print("Reading video needs OpenCV:  pip install opencv-python-headless")
        return
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps / step, (W, H))
    core = HudCore()
    i = n = 0
    t_wall = time.perf_counter()
    snaps = HERE / "hud_frames"
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            img = compose(f[..., ::-1].copy(), core, i / fps)
            arr = np.asarray(img)
            vw.write(np.ascontiguousarray(arr[..., ::-1]))
            if sheet_every and i % sheet_every == 0:
                snaps.mkdir(exist_ok=True)
                img.save(snaps / f"h{i:05d}.jpg", quality=85)
            n += 1
        i += 1
    vw.release()
    print(f"{n} frames in {time.perf_counter() - t_wall:.1f}s -> {out}")


def run_calibrate(args):
    if sys.platform != "win32":
        print("Needs Windows.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    reader = HudReader(args.hp_full)
    print("Reading the game's HUD for 10 s (switch to Roblox)...")
    with MSS() as sct:
        for _ in range(20):
            time.sleep(0.5)
            rc = roblox_client()
            if rc is None:
                print("  Roblox window not found")
                continue
            _, x, y, W, H = rc
            band, y0, top, ty0, side = grab_parts(sct, x, y, W, H, side=True)
            r = reader.read(band, y0, top, ty0, W, H, time.perf_counter(), side)
            g = reader.geo
            dashes = " ".join(f"{f:.2f}" for f, _ in r.dash)
            print(f"  HP {r.hp_sq:2d}/{N_SQ}  poise {r.poise:.2f}  "
                  f"{len(r.dash)} dashes [{dashes}]  lock {r.lock:6s}  stagger {int(r.stagger)}  "
                  f"hit {r.hit:.2f}  menu {int(r.menu)}  "
                  f"bars at x {g.left:.0f}-{g.right:.0f} y {g.top:.0f}  visible {r.visible}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="BLADE HUD for AKAYUKI")
    ap.add_argument("--demo", action="store_true", help="show every animation with fake values")
    ap.add_argument("--preview", metavar="PNG", help="draw the HUD onto a screenshot")
    ap.add_argument("--video", metavar="MP4", help="run the HUD over a recording")
    ap.add_argument("--out", help="output file for --preview / --video")
    ap.add_argument("--calibrate", action="store_true", help="print what is read from the game")
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--no-cover", action="store_true", help="don't hide the game's own bars")
    ap.add_argument("--no-fade", action="store_true", help="never dim the HUD when idle")
    ap.add_argument("--stats", action="store_true", help="print capture speed every 2 s")
    ap.add_argument("--no-chat-dodge", action="store_true",
                    help="don't slide the health cluster up when chat lines appear")
    ap.add_argument("--dxcam-path", help="folder that contains DXcam's 'dxcam' package "
                    "(found automatically if the DXcam download sits next to this folder)")
    ap.add_argument("--hp-full", choices=["dark", "red"],
                    help="what a FULL hp square looks like (default: dark, measured in game)")
    args = ap.parse_args()
    if args.preview:
        run_preview(args.preview, args.out)
    elif args.video:
        run_video(args.video, args.out or HERE / "hud_video.mp4", sheet_every=60)
    elif args.demo:
        run_demo(args)
    elif args.calibrate:
        run_calibrate(args)
    else:
        run_live(args)


if __name__ == "__main__":
    main()
