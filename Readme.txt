Tyrone S488 / MDA200 - BMC FRU inventory update
===============================================
(chassis / board / product fields - serial, asset tag, part numbers, names)

Prepared by: Shailendra

What's in this folder
---------------------
s488_tyrone_update_fru.py   the tool. one file, does everything.
fru_file/                   factory FRU images, one per board (reference copies)
Readme.txt                  this file

You need Python 3 and ipmitool. On the server both are usually already there.
If you're hitting the BMC over the network from your own machine, you need them
on that machine instead.


The short version
-----------------
Run it on the server:

    sudo ./s488_tyrone_update_fru.py --productSN "MDA200A2G-48" --assetTag "your-tag"

or from another machine, pointing at the BMC:

    ./s488_tyrone_update_fru.py --host 10.0.0.50 --user admin --pass secret \
        --productSN "MDA200A2G-48" --assetTag "your-tag"

Pass just one of --productSN / --assetTag if you're only changing one of them.
If ./ doesn't run it, use "python3 s488_tyrone_update_fru.py ..." instead, or
"chmod +x s488_tyrone_update_fru.py" once and then ./ works.

Before it writes anything, it shows you the old value next to the new one and
waits for you to type y. So a normal run looks like:

    Planned change on FRU device 0:
      Product Serial     'System serial number'  ->  'MDA200A2G-48'

    Proceed with the write? [y/N]

Type y and it does the rest, then reads the value back to prove it took. If you
don't want to be asked (say you're scripting it), add --yes. Not sure which FRU
device holds the serial? Add --auto-fru and it finds the right one for you, so
you don't have to guess a --fru number:

    sudo ./s488_tyrone_update_fru.py --auto-fru --productSN "MDA200A2G-48"

That's it for normal use. The tool sorts out the rest on its own, including the
crash we used to hit. Read on if you want to know what it's actually doing.


What the tool does under the hood
---------------------------------
There are two ways to write a FRU field, and this is the whole reason the tool
exists:

  field method - one quick "ipmitool fru edit ... field p" call. On a healthy
                 FRU it's fine. The catch is that ipmitool's FRU parser walks off
                 the end of its buffer when the product area is malformed - a
                 field whose length byte claims more bytes than the chip holds,
                 or a missing end marker - and segfaults. We hit exactly that on
                 the S488: "fru edit field" crashed, and so did plain
                 "fru print". What you're writing makes no difference; it's the
                 state of the chip, not your value.

  image method - read the entire FRU chip off the board, change the field in the
                 image, rebuild the product area with a correct length, checksum
                 and end marker, and write the whole thing back with "fru write".
                 Slower, a couple more steps, but it never goes through the buggy
                 parser, so it can't hit that crash. It also repairs the area on
                 the way through - after one image write on our box, even plain
                 "fru print" and "fru edit" started working again.

By default the tool runs in --method auto: it tries the quick field edit first,
and if that crashes or the value doesn't actually stick, it switches to the image
method by itself and finishes the job. You don't have to decide anything.

If you ever want to force one or the other:

    --method field     only try the quick edit. Fine on a healthy FRU, but it
                       crashes on a malformed one and won't fall back.
    --method image     skip straight to the reliable image method
    --method auto       the default, try field then fall back (recommended)

Changes don't show up in "System Info" until you reboot
-------------------------------------------------------
This one cost us a while, so read it before you go hunting for a bug.

The BMC web UI has two different pages that look like they show the same thing:

  FRU page          - chassis / board / product areas. This is what the tool
                      writes, and it updates the moment the write lands.
  System Info page  - Manufacturer, Model, SerialNumber, SKU, UUID, BiosVersion.
                      This one is NOT the FRU. It's SMBIOS, which the BIOS builds
                      during POST by copying values out of the FRU.

So: update the FRU, and the System Info page will still show the old values (or
"To be filled by O.E.M.") until the server reboots. That's not a failed write.
Reboot it and POST will pick the new values up. We watched it happen: after the
FRU was corrected and the box rebooted, System Info went from

    Manufacturer : To be filled by O.E.M.      ->  Tyrone Systems
    Model        : To be filled by O.E.M.      ->  Tyrone Camarero
    SerialNumber : To be filled by O.E.M.      ->  8X4081110726
    SKU          : To be filled by O.E.M.      ->  PDI200A2HG-810

on its own, with no other tool involved. If you ever read System Info while the
box is sitting in BIOS setup or mid-boot, expect the O.E.M. defaults - SMBIOS
isn't populated yet. Don't conclude the FRU didn't take.

One field is the exception: Asset Tag on the System Info page is stored by the
BMC itself, not derived from the FRU. Set that over Redfish:

    curl -sk -u <user>:<pass> -H 'Content-Type: application/json' \
         -H 'If-Match: *' -X PATCH \
         https://<BMC IP>/redfish/v1/Systems/Self \
         -d '{"AssetTag":"<your tag>"}'

That returns 204 on success. It will return 503 ("host may be in BIOS Setup")
while the machine is booting - wait for the OS and try again.


Which fields you can set
------------------------
Everything on the BMC's FRU page - chassis, board and product areas:

    Chassis:  --chassisPN        Chassis Part Number
              --chassisSN        Chassis Serial Number

    Board:    --boardMfg         Board Manufacturer
              --boardProduct     Board Product Name
              --boardSN          Board Serial Number
              --boardPN          Board Part Number
              --boardFileId      Board FRU File ID

    Product:  --productMfg       Product Manufacturer
              --productName      Product Name
              --productPN        Product Part Number
              --productVersion   Product Version
              --productSN        Product Serial Number
              --assetTag         Product Asset Tag
              --productFileId    Product FRU File ID

Pass as many as you like in one run. Anything you don't pass is left alone.

A word about space: the FRU is a small chip (256 bytes on our S488) and it's
nearly full - about 8 bytes spare. Changing a value for one of similar length is
fine, but giving a currently-empty field a real value may not fit. If it doesn't,
the tool says so and writes nothing rather than truncating your data. There's
usually room to reclaim by shortening the trailing padding other fields carry.

Other flags:

    --fru <id>   which FRU device to write. Defaults to 0. The serial usually
                 lives on the system FRU, which is normally device 0, but not
                 always. Easiest is to just use --auto-fru below. If you want to
                 look yourself, dump them with "ipmitool ... fru read <id> f.bin"
                 rather than "fru print" - print is the command that segfaults on
                 these boards.
    --auto-fru   don't bother with --fru, just find the device that holds a
                 Product Serial and use it. Handy when 0 isn't right.
    --yes, -y    skip the "Proceed? [y/N]" question. Use this when you're
                 scripting it and there's nobody there to answer.
    --no-sudo    don't put sudo in front of ipmitool in local mode. It already
                 skips sudo on Windows and when you're already root, so you
                 usually don't need this.
    --log <file> where to append the audit line. Defaults to
                 s488_fru_update.log in whatever directory you run it from.
    --no-log     don't write the audit log at all.
    --verb       show every ipmitool command and the step by step detail. Handy
                 when something isn't behaving and you want to see why.

Every successful (or failed) run drops one line into the audit log, with the
time, the host, the FRU id, and the old and new values, e.g.

    2026-07-16 16:14 | host=10.0.0.50 | fru=0 | method=field->image | \
        Product Serial: 'System serial number' -> 'MDA200A2G-48' ... | RESULT=OK

so you've got a record of what was set on which box, which is handy for the
validation paperwork.


About that "Segmentation fault" you saw
---------------------------------------
Just so it's written down somewhere: if you had ever run the old field-edit
script and seen

    Segmentation fault   ipmitool ... fru edit 0 field p 4 "MDA200A2G-48"

that was ipmitool crashing, not the script. The cause is a malformed product area
on the chip: a field's length byte claims more bytes than the FRU actually holds,
so ipmitool's parser reads past the end of its buffer and dies. It's the state of
the chip that does it, not the value you're writing, and no string length dodges
it. Same reason plain "fru print" segfaults on these boards, which is why this
tool never calls print - it reads raw dumps and decodes them itself.

You shouldn't see that crash stop you anymore. The tool detects it and switches
to the image method, which rebuilds the area properly on the way through. Worth
knowing: once an image write has cleaned the area up, ordinary "fru print" and
"fru edit" start working on that board again.


The fru_file/*.bin files
------------------------
Each .bin is a full copy of the little FRU chip on one board (the chip that holds
that board's name, part number, serial, and so on). One per board:

    System_709-S488-01S.bin        system / chassis FRU, this one has the serial
    MB_609-S4251-02S.bin           mainboard
    DC SCM_609-S405B-010.bin       DC-SCM module
    LOM Board_609-S405C-010.bin    LOM (onboard NIC) board
    PCIe carrier board_609-S405G-02S.bin   PCIe carrier
    Storage BP_609-S405T-02S.bin   storage backplane
    PDB_609-S405Q-010.bin          power distribution board
    FAN Board_609-S405J-010.bin    fan board
    FAN Board_609-S405K-010.bin    fan board

They're reference / factory copies. The serials inside them are placeholders
("System serial number", "MB serial number"), not real per-unit serials, so
don't flash one as-is expecting a real serial to appear. They're here so a board
can be re-programmed or restored if its FRU chip ever comes up blank or wrong.
The tool above doesn't need them for a normal serial update, it reads the live
image straight off the board.

They're also worth reading when you need to know how Tyrone fills a field in.
The convention across every one of them is:

    Product Manufacturer   Tyrone Systems
    Product Name           the descriptive name  (Tyrone Camarero, PDB,
                           Storage BP, MGX DC SCM, X710 LAN Board ...)
    Product Part Number    the model code        (TDA200A2R-44, MS-S405B ...)
    Product Version        the 609-/709- part number

We found .248 shipped with Product Name and Product Part Number the wrong way
round against that convention, and swapped them back.


What's actually been tested
---------------------------
Verified against a live board (172.16.15.248, an S488 / MSI S4251):

  - reading every field off a real FRU, without ever calling "fru print"
  - --auto-fru picking the right device
  - the quick field edit crashing (segfault) and the tool falling back on its own
  - the quick field edit silently NOT sticking, caught by read-back, and the
    tool falling back on its own
  - the image method writing chassis + board + product fields in one go
    (9 fields at once), all read back correct
  - repairing a bogus MultiRecord offset that ipmitool's field edit had written
    into the header
  - the backup being taken before every image write
  - FRU changes appearing in the System Info page after a reboot

Not tested yet:

  - local mode. Everything above was run remotely with --host. Running it on the
    server itself (sudo, no --host) should work but nobody has tried it.
  - any board other than the S488 / FRU device 0.


A few things worth not forgetting
---------------------------------
- When the tool uses the image method it always reads a backup of the current
  FRU first and tells you where it saved it. If a write ever goes bad, you can
  put the board back with "ipmitool fru write <id> <that-backup-file>".
- Double check the --fru id before writing. Writing the wrong image onto the
  wrong FRU will scramble that board's info.
- Remote and local do the same thing, remote just needs --host/--user/--pass.
