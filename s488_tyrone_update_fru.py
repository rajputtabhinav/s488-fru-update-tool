#!/usr/bin/env python3
#
# s488_tyrone_update_fru.py
#
# Update BMC FRU inventory fields (chassis / board / product areas) on a Tyrone
# S488 / MDA200 system, either against the local BMC or a remote one
# (ipmitool -I lanplus).
#
# Two ways to write the FRU:
#   * "field"  - ipmitool fru edit ... field <c|b|p> N <value>. One quick call,
#                and fine on a healthy FRU. But ipmitool's FRU *parser* walks off
#                the end of its buffer on a malformed area - a field whose length
#                byte overruns the chip, or a missing 0xC1 end marker - and
#                segfaults. Seen for real on the S488: both "fru edit field" AND
#                plain "fru print" crashed, while the value/length being written
#                made no difference. (Once the area was rebuilt cleanly by the
#                image method below, field edits worked again, including ones
#                that changed the string length.)
#   * "image"  - read the whole FRU, change the fields, rebuild the affected
#                areas with correct lengths + checksums + end markers, fix up the
#                common-header offsets, and write it back with fru write. Slower,
#                but it never goes through ipmitool's parser, so it can't hit
#                that crash - and it repairs the areas on the way through.
#
# By default (--method auto) it tries the quick field edit first, and if that
# crashes or doesn't stick it falls back to the image method on its own.
#
# It also:
#   * can find the right FRU device for you           (--auto-fru)
#   * shows old -> new before it writes and asks first (skip with --yes)
#   * confirms the new values by reading them back after the write
#   * appends a one-line audit record to a log file    (--log / --no-log)
#   * skips sudo automatically on Windows or when already root (--no-sudo)
#
# Examples:
#   sudo ./s488_tyrone_update_fru.py --auto-fru --productSN "MDA200A2G-48"
#   ./s488_tyrone_update_fru.py --host 10.0.0.50 --user admin --pass secret \
#       --productName "PDI200A2HG-810" --productPN "709-S488-01S" \
#       --boardSN "BR80S5B03600183"

import argparse
import datetime
import os
import platform
import shutil
import subprocess
import sys
import tempfile

END_MARKER = 0xC1
DEFAULT_LOG = "s488_fru_update.log"

# The three text areas of a FRU, per the IPMI FRU spec.
#   hdr_idx : index of this area's offset byte in the common header
#   fixed   : bytes before the first type/length field
#             chassis = ver,len,type            (3)
#             board   = ver,len,lang,date[3]    (6)
#             product = ver,len,lang            (3)
#   sect    : the letter ipmitool's "fru edit field" wants
AREA_SPEC = {
    "chassis": {"hdr_idx": 2, "fixed": 3, "sect": "c"},
    "board":   {"hdr_idx": 3, "fixed": 6, "sect": "b"},
    "product": {"hdr_idx": 4, "fixed": 3, "sect": "p"},
}

# cli dest -> (area, field index within the area, human label)
FIELDS = {
    "chassis_pn":     ("chassis", 0, "Chassis Part Number"),
    "chassis_sn":     ("chassis", 1, "Chassis Serial Number"),
    "board_mfg":      ("board",   0, "Board Manufacturer"),
    "board_product":  ("board",   1, "Board Product Name"),
    "board_sn":       ("board",   2, "Board Serial Number"),
    "board_pn":       ("board",   3, "Board Part Number"),
    "board_fileid":   ("board",   4, "Board FRU File ID"),
    "product_mfg":    ("product", 0, "Product Manufacturer"),
    "product_name":   ("product", 1, "Product Name"),
    "product_pn":     ("product", 2, "Product Part Number"),
    "product_ver":    ("product", 3, "Product Version"),
    "serial":         ("product", 4, "Product Serial"),
    "asset":          ("product", 5, "Product Asset Tag"),
    "product_fileid": ("product", 6, "Product FRU File ID"),
}

# Order used for display/audit so output reads like the BMC's FRU page.
FIELD_ORDER = ["chassis_pn", "chassis_sn",
               "board_mfg", "board_product", "board_sn", "board_pn",
               "board_fileid",
               "product_mfg", "product_name", "product_pn", "product_ver",
               "serial", "asset", "product_fileid"]

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg)


def die(msg):
    sys.stderr.write("error: " + msg + "\n")
    sys.exit(1)


# --------------------------------------------------------------------------
# ipmitool plumbing
# --------------------------------------------------------------------------
class Ipmi:
    """Holds the base ipmitool command and runs sub-commands with it."""

    def __init__(self, base, passfile=None):
        self.base = base
        self.passfile = passfile

    def run(self, args, quiet=True):
        cmd = self.base + args
        log("  running: " + " ".join(a if a else "''" for a in cmd))
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=(subprocess.PIPE if quiet else None),
            text=True,
        )

    @staticmethod
    def crashed(res):
        # Killed by a signal: Python gives a negative return code when we don't
        # go through a shell; sudo forwards it as 128 + signal (139 = SIGSEGV).
        return res.returncode < 0 or res.returncode > 128

    def close(self):
        if self.passfile and os.path.exists(self.passfile):
            os.remove(self.passfile)


def local_base(no_sudo):
    """Local ipmitool command. sudo is only prepended when it makes sense:
    not on Windows (no sudo there) and not when we're already root."""
    if no_sudo or platform.system() == "Windows":
        return ["ipmitool"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["ipmitool"]
    return ["sudo", "ipmitool"]


def build_ipmi(host, user, password, no_sudo):
    if host:
        # Password goes in a 0600 temp file (-f) so it isn't visible in `ps`.
        fd, path = tempfile.mkstemp(prefix=".ipmi_pass.")
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password)
        base = ["ipmitool", "-I", "lanplus", "-H", host, "-U", user, "-f", path]
        return Ipmi(base, passfile=path)
    return Ipmi(local_base(no_sudo))


# --------------------------------------------------------------------------
# FRU reading / decoding
# --------------------------------------------------------------------------
# NOTE: we deliberately do NOT use "ipmitool fru print" anywhere. On some BMCs
# (seen on the S488 with a malformed area) fru print segfaults while parsing.
# The raw "fru read" dump works fine, so we read the binary and decode the
# fields ourselves - same data, no crash.
def dump_fru(ipmi, fru_id):
    """Read a FRU device via 'fru read' and return its bytes, or None."""
    fd, path = tempfile.mkstemp(prefix="frudump_", suffix=".bin")
    os.close(fd)
    try:
        res = ipmi.run(["fru", "read", str(fru_id), path])
        if res.returncode != 0 or not os.path.exists(path) \
                or os.path.getsize(path) == 0:
            return None
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


def valid_fru(raw):
    return raw is not None and len(raw) >= 8 and raw[0] == 0x01


def decode_area(raw, area):
    """Return (fixed_header_bytes, [(type, data), ...]) for one FRU area, or
    (None, None) if the area isn't present. Tolerant of a missing 0xC1 marker
    and of a trailing field whose length byte overruns the chip - which is
    exactly the corruption that makes ipmitool segfault."""
    if not valid_fru(raw):
        return None, None
    spec = AREA_SPEC[area]
    off = raw[spec["hdr_idx"]] * 8
    if off == 0 or off + spec["fixed"] > len(raw):
        return None, None
    hdr = bytes(raw[off:off + spec["fixed"]])
    fields = []
    i = off + spec["fixed"]
    while i < len(raw):
        tl = raw[i]
        if tl == END_MARKER:
            break
        flen = tl & 0x3F
        data = raw[i + 1:i + 1 + flen]
        if len(data) < flen:        # length byte overruns the chip
            if data:                # keep whatever readable bytes are there
                fields.append((tl >> 6, data))
            break
        fields.append((tl >> 6, data))
        i += 1 + flen
    return hdr, fields


def field_value(raw, dest):
    """Current value of one named field, or None if absent."""
    area, idx, _ = FIELDS[dest]
    _, fields = decode_area(raw, area)
    if not fields or idx >= len(fields):
        return None
    return fields[idx][1].decode("latin1").rstrip()


def auto_detect_fru(ipmi, max_id=16):
    """Find the first FRU device that carries a non-empty Product Serial."""
    for fid in range(max_id + 1):
        raw = dump_fru(ipmi, fid)
        if raw is None:
            continue
        if (field_value(raw, "serial") or "").strip():
            log("  auto-fru: Product Serial found on FRU device %s" % fid)
            return str(fid)
    die("could not auto-detect a FRU with a Product Serial. Dump the FRUs "
        "yourself ('ipmitool ... fru read <id> f.bin') and pass --fru <id>.")


def read_current(ipmi, fru_id, updates):
    raw = dump_fru(ipmi, fru_id)
    return {k: (field_value(raw, k) if raw is not None else None)
            for k in updates}


def verify(ipmi, fru_id, updates):
    raw = dump_fru(ipmi, fru_id)
    if raw is None:
        return False
    ok = True
    for key, value in updates.items():
        actual = field_value(raw, key)
        # field_value() strips trailing padding, so compare like for like -
        # otherwise a value passed with trailing spaces would look like a FAIL
        # even though the write was fine.
        if actual == value.rstrip():
            log("  verified %s = %s" % (FIELDS[key][2], value))
        else:
            log("  %s is '%s', expected '%s'" % (FIELDS[key][2], actual, value))
            ok = False
    return ok


# --------------------------------------------------------------------------
# FRU image editing (the "image" method)
# --------------------------------------------------------------------------
def checksum(data):
    return (256 - (sum(data) % 256)) % 256


def _is_placeholder(ftype, data):
    """A field that carries no real information: empty, or a short non-ASCII
    slot the factory leaves filled with sentinel bytes (single 0xAA seen on the
    S488, sometimes 0x00 / 0xFF). Trailing runs of these can be dropped to
    reclaim space; a real string field (type 3) is never treated as one."""
    if len(data) == 0:
        return True
    return ftype != 3 and all(b in (0x00, 0xAA, 0xFF) for b in data)


def reclaim_space(fields, keep_idx, drop_placeholders):
    """Shrink one area's field list in place so an almost-full chip has room
    for a newly-populated field, without changing any field's meaning:
      * always trim trailing spaces / NULs off every string field - the tool
        already rstrips on read-back, so this loses nothing but padding; and
      * when drop_placeholders is set (only for the area actually being
        written), drop the trailing run of placeholder fields (empty slots and
        factory 0xAA/0x00/0xFF sentinels) that aren't being set in this run.
    Only trailing placeholders are removed, so the index of every field that
    still carries data - or that the caller is writing (keep_idx) - is intact.
    Returns the number of value bytes reclaimed (for logging)."""
    before = sum(1 + len(d) for _, d in fields)
    for i, (ft, data) in enumerate(fields):
        if ft == 3:
            fields[i] = (ft, data.rstrip(b" \x00"))
    while drop_placeholders and fields and (len(fields) - 1) not in keep_idx \
            and _is_placeholder(*fields[-1]):
        fields.pop()
    return before - sum(1 + len(d) for _, d in fields)


def build_area(hdr, fields):
    """Rebuild one area: fixed header + fields + end marker + pad + checksum,
    with the length byte recomputed. Total is always a multiple of 8."""
    body = bytearray(hdr)
    body[1] = 0x00                      # length placeholder
    for ftype, data in fields:
        body.append((ftype << 6) | len(data))
        body += data
    body.append(END_MARKER)
    while (len(body) + 1) % 8 != 0:
        body.append(0x00)
    body[1] = (len(body) + 1) // 8
    body.append(checksum(body))
    return bytes(body)


def _original_segments(raw):
    """Work out where each area starts/ends in the original image, so the
    opaque ones (internal use, multirecord) can be carried over untouched."""
    present = []
    for idx, name in ((1, "internal"), (2, "chassis"), (3, "board"),
                      (4, "product"), (5, "multirecord")):
        off = raw[idx] * 8
        if off:
            present.append((off, name))

    # A MultiRecord area is supposed to sit after the text areas. If its offset
    # lands on or before one of them it's overlapping something and is bogus -
    # ipmitool's "fru edit field" has been seen writing offset 1 (byte 8), right
    # on top of the internal use area. Drop it instead of relocating garbage;
    # rebuilding then clears the stale offset out of the header.
    text = [o for o, n in present if n in ("chassis", "board", "product")]
    if text:
        keep = []
        for off, name in present:
            if name == "multirecord" and off <= max(text):
                log("  note: ignoring bogus MultiRecord offset %d (overlaps the "
                    "text areas); the header will be rebuilt without it" % off)
                continue
            keep.append((off, name))
        present = keep
    present.sort()
    bounds = {}
    for i, (off, name) in enumerate(present):
        end = present[i + 1][0] if i + 1 < len(present) else len(raw)
        bounds[name] = (off, end)
    return bounds


def _assemble(raw, bounds, areas):
    """Pack the whole image from the (possibly resized) text areas, carrying the
    opaque internal-use / multirecord areas over untouched and recomputing the
    common-header offsets + checksum. Returns the bytes, not yet padded to the
    EEPROM size."""
    out = bytearray(8)
    new_off = {}
    for name in ("internal", "chassis", "board", "product", "multirecord"):
        if name in ("internal", "multirecord"):
            if name not in bounds:
                continue
            start, end = bounds[name]
            blob = bytes(raw[start:end])
        else:
            if areas[name][0] is None:
                continue
            blob = build_area(*areas[name])
        if len(out) % 8 != 0:
            out += bytes(8 - (len(out) % 8))    # areas must start on /8
        new_off[name] = len(out) // 8
        out += blob

    out[0] = 0x01
    out[1] = new_off.get("internal", 0)
    out[2] = new_off.get("chassis", 0)
    out[3] = new_off.get("board", 0)
    out[4] = new_off.get("product", 0)
    out[5] = new_off.get("multirecord", 0)
    out[6] = 0x00
    out[7] = checksum(out[:7])

    for name, off in new_off.items():
        if off > 255:
            die("FRU layout doesn't fit: %s would start past the offset limit" % name)
    return out


def edit_fru_image(raw, updates):
    """Apply {dest: value} to a FRU image and return the new image.

    Rebuilds whichever text areas are touched and reassembles the whole image,
    recomputing the common-header offsets and checksum - necessary because the
    chassis/board areas sit before the product area, so resizing one shifts
    everything after it."""
    raw = bytearray(raw)
    if not valid_fru(raw):
        die("this does not look like a FRU image (bad common header)")

    bounds = _original_segments(raw)

    # Decode the three text areas.
    areas = {}
    for name in ("chassis", "board", "product"):
        hdr, fields = decode_area(raw, name)
        areas[name] = [hdr, fields]

    # Apply the requested updates, remembering which field index we touched in
    # each area so reclaim_space() never drops one we're writing.
    set_by_area = {}
    for dest, value in updates.items():
        area, idx, label = FIELDS[dest]
        if areas[area][0] is None:
            die("this FRU has no %s area, so '%s' can't be set" % (area, label))
        data = value.encode("ascii")
        if len(data) > 63:
            die("value '%s' is too long (max 63 chars in one FRU field)" % value)
        fields = areas[area][1]
        while len(fields) <= idx:
            fields.append((3, b""))     # pad out to the field we need
        fields[idx] = (3, data)         # type 3 = 8-bit ASCII/Latin
        set_by_area.setdefault(area, set()).add(idx)

    # Reassemble once. On a chip with room to spare this is the whole story:
    # only the fields the caller asked for change, nothing else is touched.
    out = _assemble(raw, bounds, areas)

    # If it doesn't fit, the chip is nearly full - the S488 case, where giving a
    # currently-empty field (an unset File ID, a 0xAA Asset Tag placeholder) a
    # real value overruns the EEPROM. Reclaim the wasted bytes the factory image
    # carries and pack again. Trailing string padding is trimmed in every area
    # (invisible - it is stripped on read-back); trailing junk placeholder
    # fields are dropped only in the area actually being written, so areas the
    # caller didn't touch keep their exact field layout.
    if len(out) > len(raw):
        touched = set(set_by_area)
        for name in ("chassis", "board", "product"):
            if areas[name][0] is None:
                continue
            freed = reclaim_space(areas[name][1], set_by_area.get(name, set()),
                                  drop_placeholders=(name in touched))
            if freed:
                log("  reclaimed %d byte(s) from the %s area to make room"
                    % (freed, name))
        out = _assemble(raw, bounds, areas)

    if len(out) < len(raw):
        out += bytes(len(raw) - len(out))       # keep the EEPROM size
    elif len(out) > len(raw):
        die("the new values don't fit in this %d-byte FRU even after reclaiming "
            "padding (need %d). Shorten them." % (len(raw), len(out)))
    return bytes(out)


# --------------------------------------------------------------------------
# The two write methods
# --------------------------------------------------------------------------
def try_field_method(ipmi, fru_id, updates):
    crashed = False
    for key, value in updates.items():
        area, idx, label = FIELDS[key]
        sect = AREA_SPEC[area]["sect"]
        print("Updating %s via field edit ..." % label)
        res = ipmi.run(["fru", "edit", str(fru_id), "field", sect,
                        str(idx), value], quiet=not VERBOSE)
        if Ipmi.crashed(res):
            print("  ipmitool crashed (this is the known fru parser bug).")
            crashed = True
            break
        elif res.returncode != 0:
            log("  ipmitool exited non-zero; will verify before deciding.")
    if crashed:
        return False, True
    return verify(ipmi, fru_id, updates), False


def do_image_method(ipmi, fru_id, updates, host):
    print("Using the whole-image method (read / edit / write) ...")
    workdir = tempfile.mkdtemp(prefix="s488_fru_")
    newimg = os.path.join(workdir, "new.bin")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = (host or "local").replace(":", "_")
    backup = os.path.abspath("fru_backup_%s_id%s_%s.bin" % (tag, fru_id, stamp))

    # ipmitool writes the backup itself, so a non-writable working directory
    # would surface as a confusing "couldn't read the FRU" error. Check first.
    backup_dir = os.path.dirname(backup) or "."
    if not os.access(backup_dir, os.W_OK):
        die("can't save the FRU backup into %s - no write permission there. "
            "Run this from a writable directory (the backup is taken before "
            "any write, so it isn't skipped)." % backup_dir)

    res = ipmi.run(["fru", "read", str(fru_id), backup], quiet=not VERBOSE)
    if res.returncode != 0 or not os.path.exists(backup):
        die("could not read FRU %s off the BMC (%s). Nothing was written."
            % (fru_id, (res.stderr or "").strip()))
    print("  backup of current FRU saved: %s" % backup)
    print("  (restore with: ipmitool ... fru write %s %s)" % (fru_id, backup))

    with open(backup, "rb") as f:
        raw = f.read()
    with open(newimg, "wb") as f:
        f.write(edit_fru_image(raw, updates))
    print("  built new image with the requested fields")

    res = ipmi.run(["fru", "write", str(fru_id), newimg], quiet=not VERBOSE)
    if res.returncode != 0:
        die("fru write failed (%s). Your backup is at %s" %
            ((res.stderr or "").strip(), backup))
    print("  wrote the new image back")
    return verify(ipmi, fru_id, updates)


# --------------------------------------------------------------------------
# Confirmation + audit log
# --------------------------------------------------------------------------
def ordered(updates):
    return [k for k in FIELD_ORDER if k in updates]


def confirm_change(fru_id, updates, current, assume_yes):
    print("\nPlanned change on FRU device %s:" % fru_id)
    for key in ordered(updates):
        old = current.get(key)
        old = "(unset)" if old in (None, "") else old
        print("  %-22s %r  ->  %r" % (FIELDS[key][2], old, updates[key]))
    print("")
    if assume_yes:
        return
    if not sys.stdin.isatty():
        die("refusing to write without confirmation in a non-interactive run; "
            "pass --yes if you're sure.")
    try:
        ans = input("Proceed with the write? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        print("Aborted, nothing was written.")
        sys.exit(1)


def write_audit(logpath, host, fru_id, method, updates, current, final, result):
    if not logpath:
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = ["host=%s" % (host or "local"), "fru=%s" % fru_id,
             "method=%s" % method]
    for key in ordered(updates):
        parts.append("%s: %r -> %r (now %r)" %
                     (FIELDS[key][2], current.get(key), updates[key],
                      final.get(key)))
    line = "%s | %s | RESULT=%s\n" % (stamp, " | ".join(parts), result)
    try:
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(line)
        log("  audit record appended to %s" % os.path.abspath(logpath))
    except OSError as e:
        sys.stderr.write("warning: could not write audit log %s (%s)\n"
                         % (logpath, e))


# --------------------------------------------------------------------------
def main():
    global VERBOSE

    ap = argparse.ArgumentParser(
        description="Update BMC FRU chassis/board/product fields on Tyrone S488.")
    # Chassis area
    ap.add_argument("--chassisPN", dest="chassis_pn", help="Chassis Part Number")
    ap.add_argument("--chassisSN", dest="chassis_sn", help="Chassis Serial Number")
    # Board area
    ap.add_argument("--boardMfg", dest="board_mfg", help="Board Manufacturer")
    ap.add_argument("--boardProduct", dest="board_product", help="Board Product Name")
    ap.add_argument("--boardSN", dest="board_sn", help="Board Serial Number")
    ap.add_argument("--boardPN", dest="board_pn", help="Board Part Number")
    ap.add_argument("--boardFileId", dest="board_fileid", help="Board FRU File ID")
    # Product area
    ap.add_argument("--productMfg", dest="product_mfg", help="Product Manufacturer")
    ap.add_argument("--productName", dest="product_name", help="Product Name")
    ap.add_argument("--productPN", dest="product_pn", help="Product Part Number")
    ap.add_argument("--productVersion", dest="product_ver", help="Product Version")
    ap.add_argument("--productSN", dest="serial", help="Product Serial Number")
    ap.add_argument("--assetTag", dest="asset", help="Product Asset Tag")
    ap.add_argument("--productFileId", dest="product_fileid",
                    help="Product FRU File ID")
    # Everything else
    ap.add_argument("--fru", dest="fru_id", default="0",
                    help="FRU device id (default 0)")
    ap.add_argument("--auto-fru", action="store_true",
                    help="find the FRU device that has a Product Serial and use it")
    ap.add_argument("--method", choices=["auto", "field", "image"],
                    default="auto",
                    help="auto (try field, fall back to image), field, or image")
    ap.add_argument("--host", help="remote BMC IP/hostname")
    ap.add_argument("--user", help="remote BMC user (with --host)")
    ap.add_argument("--pass", dest="password", help="remote BMC password (with --host)")
    ap.add_argument("--no-sudo", action="store_true",
                    help="don't prepend sudo in local mode")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="don't ask for confirmation before writing")
    ap.add_argument("--log", dest="logpath", default=DEFAULT_LOG,
                    help="audit log file (default %s in the current dir)" % DEFAULT_LOG)
    ap.add_argument("--no-log", action="store_true", help="don't write an audit log")
    ap.add_argument("--verb", action="store_true", help="verbose output")
    args = ap.parse_args()

    VERBOSE = args.verb
    logpath = None if args.no_log else args.logpath

    updates = {}
    for dest in FIELDS:
        val = getattr(args, dest, None)
        if val is not None:
            updates[dest] = val
    if not updates:
        die("give at least one field to update, e.g. --productSN / --assetTag / "
            "--boardSN / --chassisSN (see --help for the full list).")

    remote = any([args.host, args.user, args.password])
    if remote and not all([args.host, args.user, args.password]):
        die("--host, --user and --pass must be given together for remote mode")

    if shutil.which("ipmitool") is None:
        die("ipmitool is not installed. Install it before running this.")

    ipmi = build_ipmi(args.host, args.user, args.password, args.no_sudo)

    try:
        if args.auto_fru:
            fru_id = auto_detect_fru(ipmi)
        else:
            if not str(args.fru_id).isdigit():
                die("--fru must be a non-negative integer")
            fru_id = args.fru_id

        print("Mode: %s, FRU device %s, method %s"
              % ("remote " + args.host if remote else "local", fru_id, args.method))

        current = read_current(ipmi, fru_id, updates)
        confirm_change(fru_id, updates, current, args.yes)

        used_method = args.method
        if args.method == "image":
            ok = do_image_method(ipmi, fru_id, updates, args.host)
        else:
            ok, crashed = try_field_method(ipmi, fru_id, updates)
            used_method = "field"
            if not ok and args.method == "field":
                final = read_current(ipmi, fru_id, updates)
                write_audit(logpath, args.host, fru_id, "field",
                            updates, current, final, "FAIL")
                die("field edit did not take. Re-run with --method image "
                    "(or --method auto) to use the reliable path.")
            if not ok:
                if crashed:
                    print("Falling back to the image method automatically.")
                else:
                    print("Field edit didn't stick; falling back to the image method.")
                ok = do_image_method(ipmi, fru_id, updates, args.host)
                used_method = "field->image"

        final = read_current(ipmi, fru_id, updates)
    finally:
        ipmi.close()

    write_audit(logpath, args.host, fru_id, used_method, updates, current,
                final, "OK" if ok else "FAIL")

    if ok:
        print("\nUpdated successfully. Confirmed by reading it back:")
        for key in ordered(updates):
            print("  %-22s %s" % (FIELDS[key][2], final.get(key)))
        sys.exit(0)
    else:
        # Don't suggest "fru print" here: that's the very command that segfaults
        # on these boards, which is why this tool reads raw dumps instead.
        die("update failed on FRU %s. Re-run with --auto-fru to let it find the "
            "right device, or dump the FRUs yourself with "
            "'ipmitool ... fru read <id> f.bin' to see which one holds the "
            "fields. Add --verb to see every ipmitool call." % fru_id)


if __name__ == "__main__":
    main()
