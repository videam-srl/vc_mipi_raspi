# Clear HDR Tuning Guide (IMX585)

What Clear HDR does on this sensor, what each tunable parameter controls,
and how to tune them together - for someone who knows cameras but not HDR
sensor internals.

## How it works

A single exposure can't capture both a bright highlight and a dark shadow
in the same frame if the scene's range exceeds the sensor's own dynamic
range - one clips, the other goes to black.

Clear HDR solves this with **Dual Conversion Gain (DCG)**: there's still
only **one exposure** (one integration time, one shutter setting, exactly
like a normal camera), but each photodiode's charge is read out through
**two analog gain paths at once**:

- **HCG** (High Conversion Gain): more sensitive, better for shadows, but
  saturates (clips) at a fairly low charge level.
- **LCG** (Low Conversion Gain): less sensitive, weaker signal for dim
  light, but tolerates much more charge before saturating.

This is different from sensors that use two separate exposure *times*
(DOL/Digital Overlap) - IMX585 has no second shutter-time register at
all; there's one exposure, read out twice. (Confirms this directly:
`FDG_SEL0`, the register for manually picking HCG or LCG in normal mode,
is disabled by the driver whenever Clear HDR is on, since the sensor is
already reading both paths itself.)

The sensor picks, per pixel, whichever readout is valid (HCG where it
hasn't saturated, LCG where it has), then compresses the combined result
into the 12-bit output. The Pi only ever sees this final combined image.
(This dual readout is also why Clear HDR roughly halves the max frame
rate - two analog values per pixel instead of one takes about twice as
long to read out.)

## Parameters

### `clear_hdr_mode`

HDR on/off. **Stop the stream before changing this** - it's only latched
at stream start, so it can't be changed live.

### `exposure` / `analogue_gain`

Behave as on any camera, but affect **both** readout paths together (not
one or the other), since there's only one exposure:

- `exposure` (µs): integration time. Main lever for shadow visibility.
  Pushing it too high can eventually clip HCG too, since it saturates at
  a lower charge level than LCG.
- `analogue_gain` (mdB): base gain before the readout split. Amplifies
  noise along with signal - push `exposure` first, gain second.

### `hdr_gain_adder` (EXP_GAIN, `0x3081`)

Extra gain applied to the **LCG (highlight-safe) readout** only, on top of
`analogue_gain`. `0`-`5`, roughly +6dB per step (default `2` = +12dB).
LCG's job is surviving lots of charge without saturating, which comes at
the cost of a weaker signal - this boosts that signal back up.

Raising it uses up LCG's own headroom - counterintuitively, a *higher*
adder means *less* additional `analogue_gain` you can apply before
highlights clip. To push `analogue_gain` further, try lowering
`hdr_gain_adder` first. Live-writable at any time.

### `hdr_data_sel_threshold_h` / `_l` (DATASEL_TH)

Thresholds (default `512`/`1024`) for where the per-pixel HCG/LCG
selection switches over. Live-writable.

Left at `0` (the chip's power-on default, never configured before this),
selection breaks down - the sensor favors LCG almost everywhere, so the
image behaves like a single oddly-amplified readout rather than a real
HDR combination. Keep these at sensible values.

### `hdr_data_blending_mode` (DATASEL_BK)

9-entry menu for the blend ratio at the crossover point. Default `0`.
Live-writable. Rarely needs adjustment - the thresholds do most of the
work; this just smooths or sharpens the transition.

### `hdr_gradient_compression_threshold_1` / `_2` (GRAD_TH)

Knee points (default `500`/`11500`) above which the combined signal
starts compressing to fit the 12-bit output. Live-writable. Below these,
brightness maps roughly linearly; above, a wide range of real brightness
gets squeezed into fewer output codes - this is what preserves highlight
detail instead of clipping it.

### `hdr_gradient_compression_ratio_l` / `_h` (GRAD_COMP_L/H)

Compression strength above each threshold - 12-step menu, `1/1` (none) to
`1/2048` (aggressive). Defaults `2` (1/4) and `6` (1/64). Live-writable.

**Tested finding**: pushing this more aggressive to "protect highlights
harder" doesn't reliably help and can make things worse - in testing it
flattened both bright and dark regions instead of selectively protecting
highlights. Compare against the defaults before keeping any change here.

## How they interact

```
 shadow <---------------------------------------------------> highlight
 [ HCG readout (sensitive): exposure + analogue_gain ]
              [ per-pixel selection: DATASEL_TH/BK ]
                    [ LCG readout (survives bright): hdr_gain_adder ]
                          [ compression: GRAD_TH, GRAD_COMP_L/H ]
```

- More shadow detail: raise `exposure` first, `analogue_gain` second.
- More highlight headroom at a given exposure/gain: lower
  `hdr_gain_adder` to free up LCG's remaining capacity before it clips.
- Still clipping at reasonable gain: that's the real ceiling for this
  exposure - the compression controls exist for finer adjustment, but
  treat them as delicate, not a quick fix.

## Tuning workflow

1. Start from defaults: `hdr_gain_adder=2`, curve at defaults.
2. Set `exposure` for the darkest region you need visible.
3. Check the brightest region. If clipped: lower `hdr_gain_adder`, then
   `analogue_gain` if still clipped.
4. If shadows are still too dark, raise `exposure` further before
   touching gain again.
5. Only touch `hdr_data_sel_threshold_h/l` if selection itself looks
   broken (single flat-looking readout) - a sign they're unset, not a
   day-to-day brightness knob.
6. Only touch `hdr_gradient_compression_*` last, and compare against
   defaults before keeping a change.

## Quick reference

| Control | What it changes | Live? |
|---|---|---|
| `clear_hdr_mode` | HDR on/off | No - stop stream first |
| `exposure` | Shared integration time (both readouts) | Yes |
| `analogue_gain` | Shared base gain (both readouts) | Yes |
| `hdr_gain_adder` | LCG-only gain (trades vs. highlight headroom) | Yes |
| `hdr_data_sel_threshold_h/l` | HCG/LCG selection crossover | Yes |
| `hdr_data_blending_mode` | Blend ratio at crossover | Yes |
| `hdr_gradient_compression_threshold_1/2` | Where compression starts | Yes |
| `hdr_gradient_compression_ratio_l/h` | Compression strength | Yes |

All `hdr_*` controls live on the sensor's V4L2 subdev
(`/dev/v4l-subdev2`), e.g. `v4l2-ctl -d /dev/v4l-subdev2
--set-ctrl=hdr_gain_adder=1`. The two threshold and two ratio controls
show identical truncated names in `v4l2-ctl --list-ctrls` - set them
unambiguously by numeric ID instead, e.g. `--set-ctrl=0x0098fffd=3000`.

## Combining with 2x2 binning

`binning_mode=1` (1920x1080 output) works together with Clear HDR, but
needs more `vertical_blanking` than either feature needs alone - both
features' vertical timing requirements compound. Confirmed working:
`vertical_blanking=4680` (vs. `2340` for Clear HDR alone at full
resolution). If binned + HDR frames show entirely-zero rows at the
bottom of the frame, raise `vertical_blanking` further.

## RAW10 output

Clear HDR also works with RAW10 output (`code=0x300f`, not just the
default RAW12 `0x3012`) - useful when a fixed downstream ISP (e.g. an
FPGA pipeline) only accepts RAW10 and can't be changed. Negotiate the
RAW10 format before enabling `clear_hdr_mode` (e.g. via `media-ctl`/
`v4l2-ctl --set-subdev-fmt`); the driver rejects enabling HDR if a
non-RAW10/RAW12 format is active.

RAW10 needs *more* `vertical_blanking` than RAW12 for the same feature
combination, confirmed working:
- RAW10 + Clear HDR (no binning): `vertical_blanking=4680`
- RAW10 + Clear HDR + 2x2 binning: `vertical_blanking=4680`

Both were verified on real hardware (full-frame, no zero rows/columns,
5/5 reproducible captures for the combined RAW10+HDR+binning case).
