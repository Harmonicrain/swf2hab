# Notices

## nitro-converter

`swf2hab/mapping.py` and the image naming and filtering rules in `swf2hab/convert.py` follow the
mapping logic of **nitro-converter** (Copyright the Nitro / Krews contributors, GPL-3.0,
https://git.krews.org/nitro/nitro-converter), ported from TypeScript to Python. The ported
logic covers:

- each XML element and attribute that becomes a JSON field, and its numeric/boolean parsing;
- the `_32_` / `sh_` exclusions, which apply in the `sulake` profile only;
- assigning shared bitmaps through `source`;
- the suffix scheme for duplicate animation ids.

What swf2hab changed:

- a profile that keeps everything (`full`);
- deterministic canonical-bitmap selection;
- un-premultiplication with `Math.round`, which matches Habbo's own output;
- correct decoding of `DefineBitsLossless` formats 3, 4 and 5 and of LZMA (ZWS) SWFs;
- multi-atlas packing.

## Format

The `.hab` container and the JSON layout were documented from the files Habbo serves publicly.
No Habbo code or assets are included in this repository.
