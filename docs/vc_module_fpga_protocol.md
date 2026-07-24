# VC-MIPI Module FPGA Protocol (I2C address 0x10)

This document describes what our own driver (`vc_mipi_core`) does with the
VC-MIPI module's onboard controller/FPGA at I2C address `0x10`, as opposed
to the Sony sensor itself at `0x1a`. It was written while investigating why
a third-party driver (Kurokesu's `imx585-rpi-driver`, which talks directly
to the sensor and has no concept of this module) fails to bring the sensor
up on this hardware.

## Physical picture

The camera is not a bare sensor breakout. It is a Vision Components
"VC MIPI" module: a small adapter PCB sitting between the Raspberry Pi's
CSI connector and the actual Sony IMX585 die. That adapter has its own
onboard microcontroller/FPGA which:

- gates power to the sensor,
- knows the sensor's actual bit-depth/lane/format capabilities (read out of
  an onboard descriptor table, not hardcoded in the host driver),
- generates the sensor's trigger/sync timing in some modes,
- exposes all of this over its own I2C address, `0x10`, on the same bus as
  the sensor (`0x1a`).

Our driver (`vc_mipi_camera` + `vc_mipi_core`) talks to *both* addresses.
Kurokesu's driver only knows about `0x1a` and assumes a directly-wired
sensor with the Pi's own regulator/clock/reset lines - it has no code path
that could ever touch `0x10`.

## I2C address map

| Address | Device                              |
|---------|--------------------------------------|
| `0x1a`  | Sony IMX585 sensor (passthrough)      |
| `0x10`  | VC module's own controller/FPGA       |

Source: `vc_mipi_core.c:1385` (`vc_mod_setup(ctrl, 0x10, desc)`), and the
module register map below (all defined in `vc_mipi_core.c:19-56`).

## Module register map (registers live at `0x0100`+, one byte each)

| Register | Address | Meaning |
|---|---|---|
| `MOD_REG_RESET`   | `0x0100` | Reset / power control (R/W) |
| `MOD_REG_STATUS`  | `0x0101` | Status (R) |
| `MOD_REG_MODE`    | `0x0102` | Selects which of the module's pre-programmed modes (lane count/format/type/binning) is active (R/W) |
| `MOD_REG_IOCTRL`  | `0x0103` | Input/output control - flash/trigger polarity flags (R/W) |
| `MOD_REG_MOD_ADDR`| `0x0104` | Module's own I2C address (R/W, default `0x10`) |
| `MOD_REG_SEN_ADDR`| `0x0105` | Sensor's I2C address as wired to this module (R/W, default `0x1A`) |
| `MOD_REG_OUTPUT`  | `0x0106` | Output signal override (R/W, default `0x00`) |
| `MOD_REG_INPUT`   | `0x0107` | Input signal status (R) |
| `MOD_REG_EXTTRIG` | `0x0108` | External trigger mode select (R/W, default `0x00`) |
| `MOD_REG_EXPO_L/M/H/U` | `0x0109`-`0x010C` | 32-bit exposure time for triggered modes (R/W) |
| `MOD_REG_RETRIG_L/M/H/U` | `0x010D`-`0x0110` | 32-bit retrigger interval (R/W) |
| descriptor table  | `0x1000`+ | Read-only EEPROM-style block: manufacturer, module/sensor IDs, sensor register map offsets, clock info, and the mode table (see below) |

### `MOD_REG_RESET` values
- `0x00` = `REG_RESET_PWR_UP` - power the sensor up through the module
- `0x01` = `REG_RESET_SENSOR` - hold sensor in reset
- `0x02` = `REG_RESET_PWR_DOWN` - power the sensor down

### `MOD_REG_STATUS` values
- `0x00` = `REG_STATUS_NO_COM` - no communication with sensor yet (default/transient)
- `0x80` = `REG_STATUS_READY` - sensor ready after successful internal init
- `0x01` = `REG_STATUS_ERROR` - internal error during the module's own init

### `MOD_REG_IOCTRL` / `MOD_REG_EXTTRIG` values
Flash/trigger-polarity and trigger-source flags - not relevant to plain
streaming bring-up, only to external-trigger/flash/multi-camera-sync use
cases. See `vc_mipi_core.c:44-57` for the full bit list.

## Wire protocol

Every register access (both module and sensor) uses the same 16-bit
address + 8-bit value convention (`vc_mipi_core.c:115-164`):

- **Read**: write 2 bytes (address high, address low), repeated-start, read 1 byte.
- **Write**: write 3 bytes (address high, address low, value).

This is exactly what `i2ctransfer -f -y <bus> w2@<addr> <hi> <lo> r1` /
`w3@<addr> <hi> <lo> <val>` replicate from userspace.

## Descriptor table (`0x1000`-`0x10XX`, read-only)

At probe time the driver reads this region byte-by-byte into a
`struct vc_desc` (`vc_mipi_core.h:73-114`): manufacturer string + ID,
module ID/revision, sensor manufacturer/type string, chip ID, then a block
of *sensor register offsets* (h/v start/end, output width/height, exposure,
gain - the module tells the host driver where these live in the sensor's
own register map, rather than the host hardcoding them), clock info
(`clk_ext_trigger`, `clk_pixel`), and finally a mode table: up to 24
entries of `{ data_rate, num_lanes, format, type, binning }`
(`vc_mipi_core.h:64-70`). `MOD_REG_MODE` (`0x0102`) selects an index into
this table.

Confirmed live on our hardware (`vc_mipi_core.c:1304`, logged at probe
time): manufacturer string `Vision Components`, module ID `0x0427`.

## Sequences

### 1. Probe-time bring-up (`vc_core_init()`, `vc_mipi_core.c:1378`)

1. `vc_mod_setup(ctrl, 0x10, desc)` (`vc_mipi_core.c:1257`):
   - Actively **scans** for an I2C client at `0x10` via
     `i2c_new_scanned_device`, retrying up to **200 times at 1ms
     intervals** (up to 200ms) before giving up (`vc_mipi_core.c:1094-1118`,
     `vc_mod_get_client()`). This alone shows the module's own presence on
     the bus isn't assumed to be instantaneous.
   - Reads the full descriptor table (`0x1000`+) into `desc`.
   - Reads `MOD_REG_SEN_ADDR` (`0x0105`) and cross-checks it against the
     sensor address the device-tree overlay bound this driver to - if they
     don't match, aborts with an explicit "wrong sensor manufacturer
     overlay" error.
2. `vc_mod_check_sensor_connected(client_mod)` (`vc_mipi_core.c:1239`):
   - Write `MOD_REG_RESET = 0x00` (power up).
   - Poll `MOD_REG_STATUS` every **200ms, up to 10 tries (2 seconds
     total)**, waiting for `0x80` (ready) (`vc_mod_wait_until_module_is_ready`,
     `vc_mipi_core.c:1201`).
   - Write `MOD_REG_RESET = 0x02` (power back down) regardless of outcome.
   - This is purely a connectivity check at probe time - if it fails, the
     whole driver probe aborts with "Sensor not detected".

### 2. Mode select / stream start (`vc_mod_set_mode()` → `vc_mod_reset_module()`, `vc_mipi_core.c:1489-1541`)

Runs on every `vc_sen_start_stream()` call when the requested
lanes/format/type/binning combination differs from the module's current
mode (or `FLAG_RESET_ALWAYS` is set):

1. `vc_mod_set_power(cam, 0)` - power down.
2. `vc_mod_write_mode(client, mode)` - write the mode-table index
   (`MOD_REG_MODE`, `0x0102`) selected by `vc_mod_find_mode()` by matching
   lanes/format/type/binning against the descriptor's mode table and
   picking the highest data-rate match.
3. `vc_mod_set_power(cam, 1)` - power up.
4. `vc_mod_wait_until_module_is_ready()` - same 200ms×10 poll as above.

This is the sequence that actually configures the module to route a
specific lane count/format to the CSI output, not just "power on the
sensor".

### 3. Stream stop (`vc_sen_stop_stream()`, `vc_mipi_core.c:2305`)

Does **not** power down the module. It only disables trigger mode
(`MOD_REG_EXTTRIG = 0`) and IO mode (`MOD_REG_IOCTRL = 0`), then puts the
*sensor* (not the module) into standby via the sensor's own register. The
module stays powered (`REG_RESET_PWR_UP`) across stop/start cycles - it is
only power-cycled by `vc_mod_reset_module()` when the mode actually needs
to change, or momentarily by the probe-time connectivity check.

**Implication for testing**: if our driver has run at all since the last
full power-on of the board, the module is very likely already sitting in
`REG_RESET_PWR_UP` from a previous `vc_mod_reset_module()` call - a fresh
I2C read/write to `0x10` succeeding in that state doesn't by itself prove
the module can be brought up from a **true cold/unpowered** state without
our driver's own probe sequence.

## What we actually tested against Kurokesu's driver

Kurokesu's driver never touches `0x10` at all - it drives the sensor
directly via `vana-supply`/`vdig-supply`/`vddl-supply` regulators, its own
`cam1_clk` clock request, and a reset GPIO, then talks straight to `0x1a`.

- **First test** (our overlay/driver still active from a previous boot,
  module already through at least one `vc_mod_reset_module()` cycle):
  a manual `i2ctransfer` write of `MOD_REG_RESET=0x00` followed by polling
  `MOD_REG_STATUS` succeeded on the **first** poll (already `0x80`), and a
  direct read of the sensor's blacklevel register (`0x30dc`) at `0x1a`
  then succeeded too. Per the implication above, this doesn't prove the
  module can be cold-started via I2C alone - it may just have already been
  powered up from our driver's own prior operation.

- **Second test** (fully switched to Kurokesu's overlay + driver, our
  overlay's device-tree nodes removed, board rebooted so nothing had
  touched the module since power-on): the module itself did not respond
  to *any* I2C traffic at `0x10` (`i2ctransfer` → "Remote I/O error";
  `i2cdetect` showed nothing on the whole bus). This was while
  `cam1_reg` read `disabled` in sysfs.

- We then patched Kurokesu's `imx585_power_on()` to issue our
  `vc_mod_enable_sensor()` sequence (write `MOD_REG_RESET=0x00`, poll
  `MOD_REG_STATUS`) right after their own regulator/clock/reset-GPIO
  bring-up, and independently confirmed via a tight poll loop on
  `/sys/class/regulator/regulator.9/state` and `/sys/kernel/debug/gpio`
  that `cam1_reg` genuinely does go `enabled` (GPIO line driven high) for
  roughly 600-900ms during their probe attempt. Despite the rail being
  confirmed live, our single I2C write attempt to `0x10` still failed
  (`-5`, no ACK) even after adding an extra 100ms settle delay.

- **Overlay comparison, corrected**: I initially guessed the difference
  was that our overlay enables the RP1 `i2c0mux`/`i2c0if` device-tree nodes
  and Kurokesu's doesn't - **this was wrong**. Reading Kurokesu's actual
  overlay source (`imx585-overlay.dts`, fragments `@102`/`@104`) shows it
  enables both of those exact same nodes. The two overlays' core
  I2C/CSI/regulator plumbing (`i2c_csi_dsi` target, `i2c0mux`, `i2c0if`,
  `cam1_reg`) is structurally very similar. The one thing genuinely unique
  to our overlay is a `rp1_gpio` pinctrl fragment configuring `gpio26` (or
  `gpio40` in the currently-installed `0.6.10` package) as a plain GPIO
  with no pull - labeled `cam1_trigger_gpio`. Nothing in the driver source
  actively drives this pin during bring-up though, so it's a weak lead,
  not a confirmed one.

## Retry-patience test (ruled out)

We updated the patch so the *initial* `MOD_REG_RESET` write itself retries
up to 200 times at 1ms intervals - exactly matching `vc_mod_get_client()`'s
own presence-scan loop - instead of failing after a single attempt. Result:
**every one of the 200 attempts failed identically** (`-5`, no ACK) across
the full ~1.2 second `power_on()` window, with `cam1_reg` confirmed enabled
throughout. This rules out "not enough retries" as the explanation -
lack of patience was not the problem.

## Where this leaves things

With the mux-enable theory and the retry-patience theory both ruled out
by direct testing, and `cam1_reg` confirmed physically enabled the whole
time, the remaining plausible explanations are ones that can't be
distinguished by more register-level experiments from software alone:

- Kurokesu's overlay drives `cam1_clk` onto the connector at a fixed 24MHz
  (`clk_frag`, `imx585-overlay.dts:107-113`) - our overlay never references
  a clock for the camera node at all, implying the module generates the
  sensor's clock internally. If that pin is shared with something the
  module's own oscillator/FPGA uses, having the SoC actively drive it
  could jam the module rather than help it.
- Kurokesu's driver asserts a `reset-gpios` line (`gpiod_set_value_cansleep`)
  that has no equivalent in our overlay - if that pin is actually
  multiplexed with something the module needs held in a particular state,
  driving it could have the same effect.

Telling these apart needs either the VC-MIPI module's actual schematic
(not available to us) or a logic analyzer on the physical SDA/SCL, clock,
and reset lines during a bring-up attempt - beyond what further
register/software experiments alone can resolve.

**Correction**: the `reset-gpios` theory above is wrong and should be
discarded. Kurokesu's driver requests it via
`devm_gpiod_get_optional(dev, "reset", GPIOD_OUT_HIGH)` - "optional" means
if the device-tree node has no `reset-gpios` property, this returns `NULL`
without error, and `gpiod_set_value_cansleep(NULL, ...)` is a documented
no-op. Their overlay (`imx585-overlay.dts`) never declares a `reset-gpios`
property at all, so on this exact overlay this GPIO assertion never
actually happens - it is not a live difference between the two setups.

## Pinout comparison (2026-07-24)

This was checked against the vendors' own published connector pinouts to
see whether the two boards assign the same 22-pin FPC connector
differently - which would explain the clock/reset findings above much
more concretely than guessing.

**VC MIPI module's own 22-pin connector** (Vision Components hardware
manual):

| Pin | Signal |
|---|---|
| 1,4,7,10,13,16,19 | GND |
| 2/3, 5/6, 11/12, 14/15 | CSI data lanes 0-3 (N/P pairs) |
| 8/9 | CSI_CLK (N/P) - MIPI clock lane, sensor→host, not a clock *input* |
| 17 | `trigger_to_sensor` ("not supported for all sensor modules") |
| 18 | `flash_from_sensor` ("not supported for all sensor modules") |
| 20 | I2C_SCL |
| 21 | I2C_SDA |
| 22 | Vcc3V3 |

**There is no MCLK input pin and no reset pin at all** on the VC module's
connector - consistent with everything above: the module generates the
sensor's clock internally and handles reset only via `MOD_REG_RESET` over
I2C, never via an external clock or GPIO line.

**Kurokesu's own sensor board connector** (24-pin, their wiki pinout page -
note this is Kurokesu's *board-to-board* connector for their own bare
sensor PCB, a different physical connector than the RPi CSI FFC, but it
shows what their sensor board electrically expects to receive):

| Pin | Signal |
|---|---|
| 11 | `CSI_GPIO0_RST` - reset control |
| 12 | `CAM1_MCLK` - master clock **input** |
| 13/14 | SCL/SDA |
| 22-24 | `CSI_GPIO1` (sensor power down), `CSI_GPIO2` (sensor ID), `CSI_GPIO3` (power supply control) |

Kurokesu's design is a bare-sensor breakout: **it expects the host to
supply a real MCLK and drive a reset line**, exactly the two things their
`imx585-overlay.dts` configures (`clk_frag`/`cam1_clk` at 24MHz, plus the
optional reset-gpios path that happens to be unused on this specific
overlay per the correction above).

**Conclusion**: the VC module's connector has no equivalent pin for a host
supplied MCLK at all. If the Raspberry Pi CAM1 port's own MCLK-generator
pin (driven by enabling `cam1_clk` in Kurokesu's overlay) lands - via the
straight-through FPC cable - on whatever the VC module actually has wired
at that same physical pin position, actively driving a 24MHz square wave
onto it seemed like a very plausible way to disrupt the module.

**Tested and ruled out.** We patched `imx585_power_on()`/`power_off()` to
never call `clk_prepare_enable()`/`clk_disable_unprepare()` on `xclk` at
all (kept the overlay's `cam1_clk` node at `status = "okay"` so
`devm_clk_get()` still resolves the phandle, since a `fixed-clock` node
with `status = "disabled"` would fail clock lookup entirely and break
probe earlier for an unrelated reason - only the electrical
enable/disable calls were removed). Rebuilt, rebooted into this overlay:
**identical failure** - all 200 `MOD_REG_RESET` write attempts still
returned `-5`, followed by the same `-121` on the sensor register read.
Not touching `cam1_clk` in software made no difference at all.

## Summary: three hypotheses tested, three ruled out

| Hypothesis | Test | Result |
|---|---|---|
| `i2c0mux`/`i2c0if` not enabled | Read Kurokesu's actual overlay source | Both nodes already enabled identically to our overlay - never was a real difference |
| `reset-gpios` driven where ours isn't | Checked `devm_gpiod_get_optional(dev, "reset", ...)` and the overlay | Property never declared in Kurokesu's overlay; call resolves to a no-op `NULL` descriptor - never a live difference |
| `cam1_clk` (MCLK) driven where VC module has no MCLK pin | Patched driver to skip `clk_prepare_enable`/`clk_disable_unprepare` entirely, rebuilt, rebooted | Identical failure (`-5` on all 200 tries) - ruled out |

With `cam1_reg` confirmed physically enabled (measured directly via sysfs/
gpio polling across ~700-900ms), the I2C mux enabled, no reset GPIO
actually driven, and now no clock actively driven either, the module still
never acknowledges anything at address `0x10` when brought up through
Kurokesu's overlay/driver stack. Every device-tree-level difference we
could identify and test in software has been eliminated. Whatever is
actually different between "our overlay/driver" and "Kurokesu's
overlay/driver" bring-up sequences on this exact physical board is not
visible from the device tree or driver source alone - resolving it further
would need the VC-MIPI module's real schematic or a logic analyzer on the
physical bus during a bring-up attempt, both outside what remote
software-only investigation can do.
