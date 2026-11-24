# BLADE HUD for AKAYUKI

A Phantom Blade Zero–style HUD for the Roblox game **AKAYUKI**. It hides the game's own
health, poise and dash bars and draws ink-and-steel versions over them, kept in sync
with the real ones.

It only **looks** at the screen. It never presses keys or touches the game.

---

## Quick start (3 steps)

**1. Install Python** (3.10 or newer) from python.org. Tick **"Add python.exe to PATH"**
during setup.

**2. Install the parts it needs.** Open PowerShell in this folder and run:

```
python -m pip install -r requirements.txt (if this does not work. install them seperatly what is required in the text file of "requirements")
```

**3. Start it.** Put Roblox in **windowed or borderless** mode (press **F11** in game),
then either:

- double-click **`start_hud.bat`**, or
- run `python blade_hud.py`

Keep that window open while you play. That's it.

### Controls

| Key           | Does                                              |
|---------------|---------------------------------------------------|
| **F6**        | Show / hide the HUD (hidden = the game's own bars) |
| **Ctrl + F6** | Quit                                              |

---

## What you'll see

| In the game                       | BLADE HUD shows                                                    |
|-----------------------------------|--------------------------------------------------------------------|
| 20 HP squares (bottom centre)     | **Steel health bar** beside a crest diamond. On a hit: a crimson section that drains away, a shake and a red flash. Pulses red at low health. The number under the crest is your HP squares. |
| Yellow poise line                 | **Gold poise bar** under the health bar. Turns ember-orange as it fills. |
| Poise line goes red ("Weakened")  | **Posture broken**: the gold bar shatters, a crimson ring bursts from the crest, and the bar burns red. |
| Dash pills                        | **SHIFT dash button** (bottom right): a ring with one segment per dash charge. Full ring = all dashes ready. Segments sweep back clockwise as they refill. |
| Lock-on (target name, top)        | **Q diamond** turns crimson while you're locked on.                |
| Kick (F)                          | **F diamond** flashes.                                             |
| Chat messages (bottom left)       | The health cluster slides up out of the way.                       |
| Death screen / pause menu         | The HUD fades out.                                                 |

The HUD dims when nothing is happening and comes back as soon as something does.

---

## If something's wrong

| Problem                                   | Fix                                                                 |
|-------------------------------------------|---------------------------------------------------------------------|
| `python` is not recognised                | Reinstall Python with **"Add python.exe to PATH"** ticked.          |
| Nothing shows up                          | Roblox must be **windowed / borderless**, not exclusive fullscreen (F11). |
| HUD shows two of everything               | Two copies are running. Press **Ctrl + F6**, then start one again.  |
| Bars look wrong                           | Run `python blade_hud.py --calibrate` while in game. It prints what it reads. |
| Want the game's normal bars back          | Press **F6**, or start with `--no-cover`.                           |
| The HUD isn't in OBS recordings           | Expected: it hides itself from capture so it can read the real bars underneath. |

---

## Optional: faster updates (DXcam)

By default the HUD reads the game about 35–60 times a second. To go faster, put the
`DXcam-0.3.0` folder next to this folder and run once:

```
python -m pip install comtypes
```

The HUD then prints `Capture: DXcam` at start. If **HDR is on** in Windows display
settings, DXcam sees different colours, so the HUD falls back to normal capture and
prints why. Turn HDR off if you want the extra speed.

---

## Extra options

```
python blade_hud.py --stats              print how many times a second it reads the game
python blade_hud.py --no-fade            never dim the HUD
python blade_hud.py --no-chat-dodge      don't slide up for chat
python blade_hud.py --no-cover           keep the game's own bars visible underneath
python blade_hud.py --demo               32 s of every animation with fake values
python blade_hud.py --preview shot.png   draw the HUD on a screenshot -> hud_preview.png
python blade_hud.py --video clip.mp4     HUD over a recording -> hud_video.mp4
                                         (needs: python -m pip install opencv-python-headless)
```

---

## Notes

- Needs Windows 10 (version 2004) or newer.
- Only reads the screen: no input, no game files, no network.
- The icons and frames are original drawings in that style, not game assets.
