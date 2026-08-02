#!/usr/bin/env python3
"""Interactive Elite Dangerous bindings wizard + .binds generator for HOTAS.

TUI mode (default): full-screen terminal wizard. Flow:

    1. game folder comes from --game-dir (remembered in the results file,
       so you only pass it once); the Bindings folder is derived from it
    2. pick the base preset (its bindings stay as keyboard/mouse fallback)
    3. pick target: SHIP or SRV
    4. pick a mapping section to (re)bind — or ALL

Bindings are edited in a table: every function of the section is a row
showing its current assignment straight from the results file. Keys:

    arrows  move between functions
    RETURN  (re)bind the selected function — then press the physical
            button / move the axis; after accepting, the cursor moves
            to the next row so you can chain RETURN-capture-RETURN
    I       invert an axis (stored or freshly captured)
    X       clear the binding (base preset fallback stays)
    ESC     back / cancel / redo

Results are saved after every change, so quitting any time is safe.

Generator mode (--generate): headless; builds the .binds preset from the
results file and writes it into the game's Bindings folder.

Usage:
    ed-bind-wizard.py                       # TUI wizard
    ed-bind-wizard.py --reset               # wizard from scratch
    ed-bind-wizard.py -r other.json         # use a different results file
    ed-bind-wizard.py --generate            # write the .binds preset
"""

import argparse
import curses
import fcntl
import glob
import json
import os
import select
import struct
import sys
import time
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.join(SCRIPT_DIR, "ed-bind-wizard-results.json")

STEAMAPPS = os.path.expanduser("~/.local/share/Steam/steamapps")
DEFAULT_BASE = os.path.join(
    STEAMAPPS, "common/Elite Dangerous/Products/elite-dangerous-odyssey-64/"
               "ControlSchemes/KeyboardMouseOnly.binds")
DEFAULT_BINDINGS_DIR = os.path.join(
    STEAMAPPS, "compatdata/359320/pfx/drive_c/users/steamuser/AppData/Local/"
               "Frontier Developments/Elite Dangerous/Options/Bindings")

JS_EVENT_FMT = "IhBB"          # time, value, type, number
JS_EVENT_SIZE = struct.calcsize(JS_EVENT_FMT)
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

JSIOCGAXES = 0x80016A11
JSIOCGBUTTONS = 0x80016A12
JSIOCGNAME = 0x80806A13        # 128 bytes
JSIOCGAXMAP = 0x80406A32       # u8[64]: js axis index -> ABS_* code

AXIS_THRESHOLD = 14000         # out of +-32767
DEBOUNCE = 0.7

# ABS_* code -> DirectInput axis name as ED sees it (HID-usage order)
ABS_TO_DINPUT = {0: "Joy_XAxis", 1: "Joy_YAxis", 2: "Joy_ZAxis",
                 3: "Joy_RXAxis", 4: "Joy_RYAxis", 5: "Joy_RZAxis",
                 6: "Joy_UAxis", 7: "Joy_VAxis"}

# expected raw sign for the direction prompted in the wizard; ED's pitch
# convention is opposite to the other axes (pull back = negative = pitch up)
EXPECTED_SIGN = {"PitchAxisRaw": -1, "BuggyPitchAxis": -1}

DEFAULT_DEADZONE = "0.00000000"
AXIS_DEADZONES = {              # ministicks drift more than flight axes
    "LateralThrustRaw": "0.10000000",
    "VerticalThrustRaw": "0.10000000",
    "BuggyTurretYawAxisRaw": "0.10000000",
    "BuggyTurretPitchAxisRaw": "0.10000000",
}

# target -> [(section title, [(ED function, kind, prompt), ...]), ...]
SECTIONS = {
    "ship": [
        ("Flight axes — stick", [
            ("RollAxisRaw",  "axis", "STICK: push the stick firmly RIGHT (roll)"),
            ("PitchAxisRaw", "axis", "STICK: pull the stick firmly TOWARDS you (pitch up)"),
            ("YawAxisRaw",   "axis", "YAW: twist RIGHT (twist/rudder axis) — ESC if you have none"),
        ]),
        ("Flight axes — throttle", [
            ("ThrottleAxis",      "axis", "THROTTLE: set to MINIMUM, wait 2 s, then push to MAXIMUM"),
            ("LateralThrustRaw",  "axis", "ministick: push RIGHT (lateral thrust) — optional"),
            ("VerticalThrustRaw", "axis", "ministick: push UP (vertical thrust) — optional"),
        ]),
        ("Weapons", [
            ("PrimaryFire",           "button", "trigger: PRIMARY fire"),
            ("SecondaryFire",         "button", "SECONDARY fire"),
            ("CycleFireGroupNext",    "button", "next FIRE GROUP"),
            ("CycleFireGroupPrevious","button", "previous FIRE GROUP"),
            ("DeployHardpointToggle", "button", "deploy/retract HARDPOINTS"),
        ]),
        ("Flight", [
            ("UseBoostJuice",         "button", "BOOST"),
            ("HyperSuperCombination", "button", "FSD (supercruise/hyperjump combo)"),
            ("Supercruise",           "button", "SUPERCRUISE only — optional"),
            ("Hyperspace",            "button", "HYPERSPACE JUMP only — optional"),
            ("ToggleFlightAssist",    "button", "FLIGHT ASSIST on/off"),
            ("SetSpeedZero",          "button", "full stop (0% throttle) — optional"),
            ("LandingGearToggle",     "button", "LANDING GEAR"),
            ("ToggleCargoScoop",      "button", "CARGO SCOOP"),
            ("ShipSpotLightToggle",   "button", "ship LIGHTS"),
            ("NightVisionToggle",     "button", "NIGHT VISION — optional"),
        ]),
        ("Targeting", [
            ("SelectTarget",           "button", "target AHEAD"),
            ("CycleNextTarget",        "button", "NEXT target (cycle)"),
            ("CyclePreviousTarget",    "button", "PREVIOUS target"),
            ("CycleNextHostileTarget", "button", "next HOSTILE target"),
            ("CyclePreviousHostileTarget", "button", "previous HOSTILE target"),
            ("SelectHighestThreat",    "button", "HIGHEST THREAT"),
            ("CycleNextSubsystem",     "button", "next target SUBSYSTEM"),
            ("CyclePreviousSubsystem", "button", "PREVIOUS target SUBSYSTEM"),
            ("TargetNextRouteSystem",  "button", "next system in ROUTE"),
        ]),
        ("Headlook", [
            ("HeadLookReset",        "button", "HEADLOOK reset"),
            ("HeadLookPitchAxisRaw", "axis", "headlook: push UP (look up)"),
            ("HeadLookYawAxis",      "axis", "headlook: push RIGHT (look right)"),
        ]),
        ("Power distribution (pips)", [
            ("IncreaseSystemsPower",   "button", "PIPS: SYS (systems)"),
            ("IncreaseEnginesPower",   "button", "PIPS: ENG (engines)"),
            ("IncreaseWeaponsPower",   "button", "PIPS: WEP (weapons)"),
            ("ResetPowerDistribution", "button", "PIPS: reset/balance"),
        ]),
        ("Defence", [
            ("DeployHeatSink",    "button", "HEATSINK"),
            ("FireChaffLauncher", "button", "CHAFF"),
            ("UseShieldCell",     "button", "SHIELD CELL"),
        ]),
        ("UI & panels", [
            ("UI_Up",     "button", "UI: hat UP"),
            ("UI_Down",   "button", "UI: hat DOWN"),
            ("UI_Left",   "button", "UI: hat LEFT"),
            ("UI_Right",  "button", "UI: hat RIGHT"),
            ("UI_Select", "button", "UI: SELECT (e.g. hat press)"),
            ("UI_Back",   "button", "UI: BACK"),
            ("FocusLeftPanel",  "button", "LEFT panel (nav/contacts)"),
            ("FocusRightPanel", "button", "RIGHT panel (systems)"),
            ("FocusCommsPanel", "button", "COMMS panel — optional"),
            ("GalaxyMapOpen",   "button", "GALAXY MAP — optional"),
            ("SystemMapOpen",   "button", "SYSTEM MAP — optional"),
        ]),
    ],
    "srv": [
        ("SRV driving", [
            ("SteeringAxis",   "axis", "SRV STEERING: steer RIGHT"),
            ("DriveSpeedAxis", "axis", "SRV THROTTLE: set to MINIMUM, wait 2 s, then MAXIMUM"),
            ("BuggyRollAxisRaw", "axis", "SRV ROLL (airborne): push RIGHT — optional"),
            ("BuggyPitchAxis",   "axis", "SRV PITCH (airborne): pull TOWARDS you (nose up) — optional"),
            ("VerticalThrustersButton",        "button", "SRV vertical THRUSTERS"),
            ("ToggleDriveAssist",              "button", "DRIVE ASSIST on/off"),
            ("AutoBreakBuggyButton",           "button", "HANDBRAKE"),
            ("BuggyToggleReverseThrottleInput","button", "toggle REVERSE"),
            ("HeadlightsBuggyButton",          "button", "HEADLIGHTS"),
            ("RecallDismissShip",              "button", "RECALL/DISMISS ship"),
        ]),
        ("SRV turret & combat", [
            ("BuggyPrimaryFireButton",   "button", "SRV PRIMARY fire"),
            ("BuggySecondaryFireButton", "button", "SRV SECONDARY fire"),
            ("ToggleBuggyTurretButton",  "button", "TURRET mode toggle"),
            ("SelectTarget_Buggy",       "button", "select TARGET"),
            ("BuggyTurretYawAxisRaw",    "axis", "TURRET YAW: push RIGHT — optional"),
            ("BuggyTurretPitchAxisRaw",  "axis", "TURRET PITCH: pull TOWARDS you (up) — optional"),
        ]),
        ("SRV pips & cargo", [
            ("IncreaseSystemsPower_Buggy",   "button", "PIPS: SYS"),
            ("IncreaseEnginesPower_Buggy",   "button", "PIPS: ENG"),
            ("IncreaseWeaponsPower_Buggy",   "button", "PIPS: WEP"),
            ("ResetPowerDistribution_Buggy", "button", "PIPS: reset/balance"),
            ("ToggleCargoScoop_Buggy",       "button", "CARGO SCOOP"),
            ("EjectAllCargo_Buggy",          "button", "eject all cargo — optional"),
        ]),
        ("SRV UI & panels", [
            ("FocusLeftPanel_Buggy",  "button", "LEFT panel"),
            ("FocusRightPanel_Buggy", "button", "RIGHT panel"),
            ("FocusCommsPanel_Buggy", "button", "COMMS panel — optional"),
            ("GalaxyMapOpen_Buggy",   "button", "GALAXY MAP — optional"),
            ("SystemMapOpen_Buggy",   "button", "SYSTEM MAP — optional"),
        ]),
    ],
}


# ---------------------------------------------------------------- devices --

class Device:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        buf = bytearray(128)
        fcntl.ioctl(self.fd, JSIOCGNAME, buf)
        self.name = buf.split(b"\0", 1)[0].decode(errors="replace")
        self.n_axes = struct.unpack("B", fcntl.ioctl(self.fd, JSIOCGAXES, b"\0"))[0]
        self.n_buttons = struct.unpack("B", fcntl.ioctl(self.fd, JSIOCGBUTTONS, b"\0"))[0]
        self.axis_vals = {}
        self.role = None           # assigned during detection

    def read_events(self):
        events = []
        while True:
            try:
                data = os.read(self.fd, JS_EVENT_SIZE * 64)
            except BlockingIOError:
                break
            if not data:
                break
            for off in range(0, len(data) - JS_EVENT_SIZE + 1, JS_EVENT_SIZE):
                _, value, etype, number = struct.unpack_from(JS_EVENT_FMT, data, off)
                init = bool(etype & JS_EVENT_INIT)
                etype &= ~JS_EVENT_INIT
                if etype == JS_EVENT_AXIS:
                    self.axis_vals[number] = value
                    if not init:
                        events.append(("axis", number, value))
                elif etype == JS_EVENT_BUTTON and not init:
                    events.append(("button", number, value))
        return events


def proc_joysticks():
    """name -> {vid, pid, js} for devices with a js handler."""
    out = {}
    with open("/proc/bus/input/devices") as f:
        for blk in f.read().split("\n\n"):
            name = vid = pid = jsdev = None
            for line in blk.splitlines():
                if line.startswith("I:"):
                    for part in line.split():
                        if part.startswith("Vendor="):
                            vid = part[7:]
                        elif part.startswith("Product="):
                            pid = part[8:]
                elif line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip('"')
                elif line.startswith("H: Handlers="):
                    for h in line.split("=", 1)[1].split():
                        if h.startswith("js"):
                            jsdev = "/dev/input/" + h
            if jsdev and name and vid and pid:
                out[name] = {"vid": vid, "pid": pid, "js": jsdev}
    return out


def axis_map(fd, n_axes):
    """js axis index -> ABS_* code, via JSIOCGAXMAP."""
    buf = bytearray(64)
    fcntl.ioctl(fd, JSIOCGAXMAP, buf)
    return list(buf[:n_axes])


def describe(r):
    if not r:
        return "(skipped)"
    if r["type"] == "button":
        return f"{r['role']} button {r['index'] + 1}"
    return f"{r['role']} axis {r['index']} ({'+' if r['sign'] > 0 else '-'})"


def save(results, path):
    with open(path, "w") as f:
        json.dump(results, f, indent=2)


# --------------------------------------------------------------- generator --

def resolve_devices(results):
    """role -> {'id': 'VIDPID' uppercase hex (as ED writes it, e.g.
    '334443E8'), 'axmap': [ABS codes by js axis index]}."""
    out = {}
    joys = proc_joysticks()
    saved = results.get("_devices", {})
    for role in ("stick", "throttle"):
        info = dict(saved.get(role, {}))
        live = joys.get(info.get("name"))
        if not (info.get("vid") and info.get("pid")) and live:
            info["vid"], info["pid"] = live["vid"], live["pid"]
        if not info.get("axmap") and live:
            dev = Device(live["js"])
            info["axmap"] = axis_map(dev.fd, dev.n_axes)
            os.close(dev.fd)
        if info.get("vid") and info.get("pid"):
            out[role] = {"id": (info["vid"] + info["pid"]).upper(),
                         "axmap": info.get("axmap")}
    if len(out) < 2:
        # no _devices in the results file — classify what is plugged in now
        for name, j in joys.items():
            role = "throttle" if "throttle" in name.lower() else "stick"
            if role in out:
                continue
            dev = Device(j["js"])
            out[role] = {"id": (j["vid"] + j["pid"]).upper(),
                         "axmap": axis_map(dev.fd, dev.n_axes)}
            os.close(dev.fd)
    missing = {"stick", "throttle"} - set(out)
    if missing:
        raise RuntimeError(f"cannot resolve devices for: "
                           f"{', '.join(missing)} — plug the devices in "
                           f"or re-run the wizard")
    return out


def generate(results, base, bindings_dir, preset_name):
    """Build the .binds file. Returns human-readable summary lines."""
    devs = resolve_devices(results)
    lines = [f"device ids: stick={devs['stick']['id']} "
             f"throttle={devs['throttle']['id']}"]

    tree = ET.parse(base)
    root = tree.getroot()
    root.set("PresetName", preset_name)
    root.set("MajorVersion", "4")
    root.set("MinorVersion", "2")

    bound, created, skipped = [], [], []
    for func, r in results.items():
        if func.startswith("_"):
            continue
        if not r:
            skipped.append(func)
            continue
        device = devs[r["role"]]["id"]
        el = root.find(func)
        if el is None:
            el = ET.SubElement(root, func)
            created.append(func)
        if r["type"] == "button":
            key = f"Joy_{r['index'] + 1}"
            primary = el.find("Primary")
            secondary = el.find("Secondary")
            if primary is None:
                primary = ET.SubElement(el, "Primary")
            if secondary is None:
                secondary = ET.SubElement(el, "Secondary",
                                          Device="{NoDevice}", Key="")
            # keep the base (keyboard) binding as a fallback on Secondary
            if (primary.get("Device") not in (None, "{NoDevice}")
                    and secondary.get("Device") in (None, "{NoDevice}")):
                secondary.attrib.clear()
                secondary.attrib.update(primary.attrib)
            primary.attrib.clear()
            primary.set("Device", device)
            primary.set("Key", key)
        else:
            axmap = devs[r["role"]]["axmap"]
            if (not axmap or r["index"] >= len(axmap)
                    or axmap[r["index"]] not in ABS_TO_DINPUT):
                raise RuntimeError(f"{func}: cannot map {r['role']} axis "
                                   f"{r['index']} — axmap={axmap}")
            key = ABS_TO_DINPUT[axmap[r["index"]]]
            binding = el.find("Binding")
            if binding is None:
                binding = ET.SubElement(el, "Binding")
            binding.attrib.clear()
            binding.set("Device", device)
            binding.set("Key", key)
            inverted = el.find("Inverted")
            if inverted is None:
                inverted = ET.SubElement(el, "Inverted")
            expected = EXPECTED_SIGN.get(func, 1)
            inverted.set("Value", "0" if r["sign"] == expected else "1")
            deadzone = el.find("Deadzone")
            if deadzone is None:
                deadzone = ET.SubElement(el, "Deadzone")
            deadzone.set("Value", AXIS_DEADZONES.get(func, DEFAULT_DEADZONE))
        bound.append(func)

    out = os.path.join(bindings_dir, f"{preset_name}.4.2.binds")
    ET.indent(tree, space="\t")
    tree.write(out, encoding="utf-8", xml_declaration=True)

    lines.append(f"wrote {len(bound)} bindings -> {out}")
    if created:
        lines.append(f"functions absent from the base preset (added fresh): "
                     f"{', '.join(created)}")
    if skipped:
        lines.append(f"skipped in wizard (base bindings only): "
                     f"{', '.join(skipped)}")
    lines.append(f"In game: Options -> Controls -> preset '{preset_name}'")
    return lines


# -------------------------------------------------------------------- TUI --

class Tui:
    def __init__(self, scr):
        self.scr = scr
        self.title = ""
        self.lines = []

    def key(self, timeout=0.0):
        """'enter' / 'esc' / 'up' / 'down' / printable char / None."""
        deadline = time.monotonic() + timeout
        while True:
            c = self.scr.getch()
            if c == -1:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.02)
                continue
            if c in (10, 13, curses.KEY_ENTER):
                return "enter"
            if c == 27:
                return "esc"
            if c == curses.KEY_UP:
                return "up"
            if c == curses.KEY_DOWN:
                return "down"
            if 32 <= c < 127:
                return chr(c)
            # ignore resize and anything exotic

    def _put(self, y, x, text, attr=curses.A_NORMAL):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h:
            try:
                self.scr.addstr(y, x, text[:max(0, w - x - 1)], attr)
            except curses.error:
                pass

    def menu(self, title, items, index=0, footer="arrows = move, "
             "RETURN = select, ESC = back"):
        index = max(0, min(index, len(items) - 1))
        top = 0
        while True:
            h, _ = self.scr.getmaxyx()
            visible = max(3, h - 4)
            if index < top:
                top = index
            elif index >= top + visible:
                top = index - visible + 1
            self.scr.erase()
            self._put(0, 0, title, curses.A_BOLD)
            for row, i in enumerate(range(top,
                                          min(len(items), top + visible))):
                attr = curses.A_REVERSE if i == index else curses.A_NORMAL
                self._put(2 + row, 2, items[i], attr)
            self._put(h - 1, 0, f"{footer}  ({index + 1}/{len(items)})")
            self.scr.refresh()
            k = self.key(0.5)
            if k == "up":
                index = (index - 1) % len(items)
            elif k == "down":
                index = (index + 1) % len(items)
            elif k == "enter":
                return index
            elif k == "esc":
                return None

    def page(self, title):
        self.title = title
        self.lines = []
        self._redraw()

    def log(self, line=""):
        self.lines.append(line)
        self._redraw()

    def _redraw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        self._put(0, 0, self.title, curses.A_BOLD)
        for i, ln in enumerate(self.lines[-(h - 2):]):
            self._put(2 + i, 0, ln)
        self.scr.refresh()

    def wait_any_key(self):
        self.log("")
        self.log("-- press RETURN or ESC to continue --")
        while self.key(0.5) not in ("enter", "esc"):
            pass


# ----------------------------------------------------------- capture logic --

def drain(devices, tui, seconds=DEBOUNCE):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([d.fd for d in devices], [], [], 0.05)
        for d in devices:
            if d.fd in r:
                d.read_events()
    while tui.key(0.0):
        pass


def wait_input(devices, want_axis, tui):
    """Wait for a joystick press / axis move, or ESC. No time limit.

    Returns (dev, kind, index, sign) or "skip".
    """
    for d in devices:
        d.read_events()
    baseline = {d.path: dict(d.axis_vals) for d in devices}
    while True:
        r, _, _ = select.select([d.fd for d in devices], [], [], 0.05)
        if tui.key(0.0) == "esc":
            return "skip"
        for d in devices:
            if d.fd not in r:
                continue
            for kind, number, value in d.read_events():
                if kind == "button" and value == 1:
                    return d, "button", number, 1
                if kind == "axis" and want_axis:
                    base = baseline[d.path].get(number, 0)
                    delta = value - base
                    if abs(delta) > AXIS_THRESHOLD:
                        return d, "axis", number, (1 if delta > 0 else -1)


def detect_device(devices, label, tui):
    tui.log(f"--> press any button on your {label}:")
    while True:
        got = wait_input(devices, want_axis=False, tui=tui)
        if got == "skip":
            continue                       # ESC is meaningless here
        d = got[0]
        if d.role is not None:
            tui.log(f"    that came from the {d.role} — try again on "
                    f"the {label}")
            drain(devices, tui)
            continue
        tui.log(f"    OK: {d.name}")
        tui.log("")
        drain(devices, tui)
        return d


# ------------------------------------------------------------------- flows --

def resolve_config(args, cfg):
    """Validate --game-dir / remembered config; exits with a clear message.

    Returns cfg with game_dir, schemes_dir and bindings_dir filled in.
    """
    game = args.game_dir or cfg.get("game_dir")
    if not game:
        sys.exit("Pass --game-dir /path/to/steamapps/common/'Elite "
                 "Dangerous' (remembered in the results file afterwards).")
    game = os.path.abspath(os.path.expanduser(game))
    schemes = os.path.join(
        game, "Products", "elite-dangerous-odyssey-64", "ControlSchemes")
    if not os.path.isdir(schemes):
        sys.exit(f"{game}\ndoes not look like an Elite Dangerous install "
                 f"(missing Products/elite-dangerous-odyssey-64/"
                 f"ControlSchemes).")
    # <steamapps>/common/<game> -> <steamapps>/compatdata/359320/...
    steamapps = os.path.dirname(os.path.dirname(game))
    derived = os.path.join(
        steamapps, "compatdata", "359320", "pfx", "drive_c", "users",
        "steamuser", "AppData", "Local", "Frontier Developments",
        "Elite Dangerous", "Options", "Bindings")
    bindings = args.bindings_dir or (cfg.get("bindings_dir")
                                     if not args.game_dir else None) or derived
    if not os.path.isdir(bindings):
        sys.exit(f"Bindings folder not found:\n{bindings}\n"
                 f"Run the game once so it creates it, or pass "
                 f"--bindings-dir.")
    cfg.update({"game_dir": game, "schemes_dir": schemes,
                "bindings_dir": bindings})
    return cfg


def screen_base_preset(tui, cfg):
    entries = []
    schemes = sorted(glob.glob(os.path.join(cfg["schemes_dir"], "*.binds")))
    for p in schemes:
        entries.append((os.path.basename(p), p))
    customs = sorted(glob.glob(os.path.join(cfg["bindings_dir"], "*.binds")))
    for p in customs:
        entries.append((f"custom: {os.path.basename(p)}", p))
    if not entries:
        tui.page("Base preset")
        tui.log(f"no .binds found in {cfg['schemes_dir']}")
        tui.wait_any_key()
        return False
    index = 0
    for i, (_, p) in enumerate(entries):
        if p == cfg.get("base") or (not cfg.get("base")
                                    and "KeyboardMouseOnly" in p):
            index = i
    choice = tui.menu("Base preset (its bindings stay as fallback)",
                      [label for label, _ in entries], index)
    if choice is None:
        return False
    cfg["base"] = entries[choice][1]
    return True


def detect_devices_screen(tui):
    devices = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            devices.append(Device(path))
        except OSError:
            pass
    tui.page("Device detection")
    for d in devices:
        tui.log(f"  {d.path}: {d.name}  ({d.n_axes} axes, "
                f"{d.n_buttons} buttons)")
    tui.log("")
    if len(devices) < 2:
        tui.log("Need at least two joystick devices — check connections.")
        tui.wait_any_key()
        return None
    stick = detect_device(devices, "FLIGHT STICK", tui)
    stick.role = "stick"
    throttle = detect_device(devices, "THROTTLE", tui)
    throttle.role = "throttle"
    return [stick, throttle]


def section_stats(results, items):
    """(bound, skipped, total) for one section's items."""
    bound = sum(1 for f, _, _ in items if results.get(f))
    skipped = sum(1 for f, _, _ in items if f in results and not results[f])
    return bound, skipped, len(items)


def target_stats(results, target):
    bound = skipped = total = 0
    for _, items in SECTIONS[target]:
        b, s, n = section_stats(results, items)
        bound, skipped, total = bound + b, skipped + s, total + n
    return bound, skipped, total


def progress_label(title, bound, skipped, total):
    label = f"{title}  [{bound}/{total}"
    if skipped:
        label += f", {skipped} skipped"
    return label + "]"


def run_table(tui, devices, results, used, sections, path, heading):
    """Arrow-key table over all functions of the given sections."""
    rows = []                             # ("header", title) / ("item", ...)
    for sec_title, items in sections:
        rows.append(("header", sec_title, None, None))
        for func, kind, desc in items:
            rows.append(("item", func, kind, desc))
    item_rows = [i for i, r in enumerate(rows) if r[0] == "item"]
    if not item_rows:
        return
    sel = item_rows[0]
    state = {"top": 0}

    def move(step):
        nonlocal sel
        pos = item_rows.index(sel)
        pos = max(0, min(len(item_rows) - 1, pos + step))
        sel = item_rows[pos]

    def draw(status_lines):
        h, _ = tui.scr.getmaxyx()
        visible = max(4, h - 6)
        top = state["top"]
        if sel < top:
            top = sel
        elif sel >= top + visible:
            top = sel - visible + 1
        state["top"] = top
        tui.scr.erase()
        tui._put(0, 0, heading, curses.A_BOLD)
        for row, i in enumerate(range(top, min(len(rows), top + visible))):
            what, a, _, _ = rows[i]
            y = 2 + row
            if what == "header":
                tui._put(y, 0, f"--- {a} ---", curses.A_BOLD)
            else:
                current = (describe(results[a]) if a in results
                           else "(unset)")
                attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
                tui._put(y, 2, f"{a:32s} {current}", attr)
        for j, line in enumerate(status_lines[:2]):
            tui._put(h - 3 + j, 0, line)
        tui._put(h - 1, 0, "arrows = move, RETURN = bind, I = invert, "
                           "X = clear, ESC = back")
        tui.scr.refresh()

    status = []
    while True:
        draw(status)
        k = tui.key(0.5)
        if k is None:
            continue
        if k == "esc":
            return
        if k == "up":
            move(-1)
            status = []
        elif k == "down":
            move(+1)
            status = []
        elif k in ("x", "X"):
            _, func, _, _ = rows[sel]
            results[func] = None
            save(results, path)
            status = [f"{func}: cleared (base preset binding stays)"]
        elif k in ("i", "I"):
            _, func, _, _ = rows[sel]
            r = results.get(func)
            if r and r["type"] == "axis":
                r["sign"] = -r["sign"]
                save(results, path)
                status = [f"{func}: inverted -> {describe(r)}"]
        elif k == "enter":
            _, func, kind, desc = rows[sel]
            while True:
                draw([f"-> {desc}", "   waiting for input...  (ESC = cancel)"])
                got = wait_input(devices, want_axis=(kind == "axis"), tui=tui)
                if got == "skip":                  # ESC = cancel capture
                    status = [f"{func}: unchanged"]
                    break
                d, etype, number, sign = got
                key_ = (d.role, etype, number)
                dup = (f"  WARNING: same as {used[key_]}!"
                       if key_ in used and etype == "button" else "")
                drain(devices, tui)
                accept = None
                while accept is None:
                    label = (f"button {number + 1}" if etype == "button"
                             else f"axis {number} "
                                  f"({'+' if sign > 0 else '-'})")
                    opts = ("[RETURN = accept, ESC = redo, I = invert]"
                            if etype == "axis"
                            else "[RETURN = accept, ESC = redo]")
                    draw([f"-> {desc}",
                          f"   captured: {d.role} {label}{dup}  {opts}"])
                    kk = tui.key(0.5)
                    if kk == "enter":
                        accept = True
                    elif kk == "esc":
                        accept = False
                    elif kk in ("i", "I") and etype == "axis":
                        sign = -sign
                if accept:
                    used.setdefault(key_, func)
                    results[func] = {"role": d.role, "type": etype,
                                     "index": number, "sign": sign}
                    save(results, path)
                    status = [f"{func}: {describe(results[func])}"]
                    move(+1)                       # the NEXT affordance
                    break
            drain(devices, tui)


def tui_main(scr, args, results, cfg):
    curses.curs_set(0)
    scr.nodelay(True)
    scr.keypad(True)
    try:
        curses.set_escdelay(50)
    except AttributeError:
        pass
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        pass
    tui = Tui(scr)

    if not screen_base_preset(tui, cfg):
        return
    results["_config"] = cfg
    save(results, args.results)

    active = detect_devices_screen(tui)
    if active is None:
        return
    joys = proc_joysticks()
    results["_devices"] = {
        d.role: {"name": d.name,
                 "vid": joys.get(d.name, {}).get("vid", ""),
                 "pid": joys.get(d.name, {}).get("pid", ""),
                 "axmap": axis_map(d.fd, d.n_axes)}
        for d in active}
    save(results, args.results)

    # duplicate detection seeded with what is already recorded
    used = {}
    for func, r in results.items():
        if not func.startswith("_") and r and r["type"] == "button":
            used.setdefault((r["role"], "button", r["index"]), func)

    while True:
        choice = tui.menu("ed-bind-wizard — main menu", [
            progress_label("Bind SHIP controls",
                           *target_stats(results, "ship")),
            progress_label("Bind SRV controls",
                           *target_stats(results, "srv")),
            "Generate .binds",
            "Quit",
        ])
        if choice in (None, 3):
            return
        if choice == 2:
            tui.page("Generate .binds")
            try:
                for line in generate(results, cfg["base"],
                                     cfg["bindings_dir"], args.preset_name):
                    tui.log(line)
            except (RuntimeError, OSError, ET.ParseError) as e:
                tui.log(f"ERROR: {e}")
            tui.wait_any_key()
            continue
        target = "ship" if choice == 0 else "srv"
        while True:
            secs = SECTIONS[target]
            labels = ([progress_label("ALL sections",
                                      *target_stats(results, target))]
                      + [progress_label(title, *section_stats(results, items))
                         for title, items in secs]
                      + ["<- back"])
            sc = tui.menu(f"{target.upper()} — mapping sections", labels)
            if sc is None or sc == len(labels) - 1:
                break
            chosen = secs if sc == 0 else [secs[sc - 1]]
            heading = (f"{target.upper()} — "
                       f"{'all sections' if sc == 0 else chosen[0][0]}")
            run_table(tui, active, results, used, chosen, args.results,
                      heading)


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        description="Elite Dangerous HOTAS bindings wizard and generator")
    ap.add_argument("-r", "--results", default=DEFAULT_RESULTS,
                    help="results JSON: wizard state / generator input "
                         "(default: next to this script)")
    ap.add_argument("--reset", action="store_true",
                    help="delete the results file and start from scratch")
    ap.add_argument("-g", "--generate", action="store_true",
                    help="generate the .binds preset from results and exit")
    ap.add_argument("--preset-name", default="Izowiuz-VIRPIL",
                    help="preset name shown in the game dropdown")
    ap.add_argument("--game-dir", default=None,
                    help="Elite Dangerous game folder "
                         "(steamapps/common/Elite Dangerous); required on "
                         "first TUI run, remembered afterwards")
    ap.add_argument("--base", default=None,
                    help="base .binds preset to build on "
                         "(default: the one picked in the TUI)")
    ap.add_argument("--bindings-dir", default=None,
                    help="where to write the generated .binds "
                         "(default: the folder picked in the TUI)")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.results):
        os.remove(args.results)

    if args.generate:
        with open(args.results) as f:
            results = json.load(f)
        cfg = results.get("_config", {})
        base = args.base or cfg.get("base") or DEFAULT_BASE
        bindings_dir = (args.bindings_dir or cfg.get("bindings_dir")
                        or DEFAULT_BINDINGS_DIR)
        try:
            for line in generate(results, base, bindings_dir,
                                 args.preset_name):
                print(line)
        except (RuntimeError, OSError, ET.ParseError) as e:
            sys.exit(f"ERROR: {e}")
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("Run this in a regular terminal (the wizard is a TUI), "
                 "or use --generate for headless generation.")

    results = {}
    if os.path.exists(args.results):
        with open(args.results) as f:
            results = json.load(f)
        results.pop("_skip", None)         # from an older wizard version
    cfg = resolve_config(args, dict(results.get("_config", {})))
    curses.wrapper(tui_main, args, results, cfg)


if __name__ == "__main__":
    main()
