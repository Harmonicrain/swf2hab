# swf2hab

Converts Habbo Flash asset libraries (`.swf`) into `.hab` bundles, the format Habbo's own HTML5
client loads instead of SWFs. That covers furni, pets, figure parts, effects and room content.
It also converts Nitro bundles (`.nitro`) into `.hab` (see [From .nitro](#from-nitro)), and turns
`.hab` bundles back into Flash libraries or Nitro bundles (see [Back to .swf and .nitro](#back-to-swf-and-nitro)).

- **Standalone.** Python 3.9+ standard library only. Installing `numpy` and `Pillow` makes it
  faster and the PNGs smaller, but the decoded pixels are the same either way.
- **Checked against Habbo.** Run on Habbo's own `.swf` files, it reproduces the `.hab` files
  Habbo serves from `images.habbo.com`. On a test set of 75 libraries (61 furni plus figure,
  pet, effect and placeholder libraries), every JSON document matched and all 7,333 frames were
  pixel-identical, and the output was about 7% smaller. To repeat the check, see
  `tools/oracle_check.py`.
- **Safe.** It only reads your SWFs (or .nitro bundles) and writes into a separate output folder.

## Install

```
pip install .            # or just run it from this folder: python -m swf2hab ...
pip install .[fast]      # optional: numpy + Pillow
```

## Use

```
# one file
python -m swf2hab convert throne.swf -o out/

# whole folders; the folder structure is mirrored, nothing is written next to the SWFs
python -m swf2hab convert C:/habbo/dcr/hof_furni -o C:/habbo/hab/dcr/hof_furni --report furni.json
python -m swf2hab convert C:/habbo/gordon/PRODUCTION-... -o C:/habbo/hab/gordon/PRODUCTION-... \
        --exclude Habbo.swf --exclude HabboAir.swf

python -m swf2hab inspect throne.swf out/throne.hab     # what is inside
python -m swf2hab unpack out/throne.hab -o throne/      # extract entries
python -m swf2hab verify out/                           # validate like the client does
python -m swf2hab compare mine.hab habbos.hab           # JSON + per-frame pixel diff

python -m swf2hab export out/ --to swf -o swf/          # .hab (or .nitro) -> Flash asset library
python -m swf2hab export bundled/ --to nitro -o nitro/  # .hab (or .swf) -> Nitro bundle
```

The defaults are all CPU cores, incremental runs (a `.hab` newer than its `.swf` is skipped
unless you pass `--force`), and atlases of at most 8192 px a side (`--max-atlas`).

### Profiles

| `--profile` | What goes in | Use it for |
|---|---|---|
| `full` (default) | Habbo's JSON, an atlas for normal-size art, a separate `-32` atlas for zoomed-out art (32px furni, `sh_` figure parts), and every raw XML/binary symbol | Clients that run the original AS3 room engine (e.g. SkyHaxe). Nothing is lost. |
| `sulake` | Exactly what Habbo ships: JSON plus one atlas of 64px and icon art. 32px and `sh_` art is dropped, as Habbo does. Pet palettes are kept as raw entries. | Habbo's HTML5 client, or anything that expects their files byte-for-byte in structure |

### Layouts

`--layout auto` picks one of these per file:

- **atlas**: `<name>.json` + `<name>.png`. Used for furni, pets, figure libraries and effects.
- **library**: the manifest XML sits in the index, with one entry per symbol (XML, one PNG per
  bitmap, MP3). Habbo uses this for room content such as `HabboRoomContent` and for UI
  libraries. `auto` uses it for room content (`visualization="room"`) and for SWFs that have no
  Habbo manifest.

## From .nitro

`convert` also accepts `.nitro` files and folders that contain them, mixed with SWFs if you like:

```
python -m swf2hab convert nitro-react/dist/bundled -o hab/ --report nitro.json
python -m swf2hab inspect throne.nitro          # list the files inside a .nitro
```

A `.nitro` (made by nitro-converter) already holds the JSON layout Habbo's `.hab` files use, plus
its atlas PNG, so it is repackaged rather than re-rendered: the PNG is copied byte for byte. Two
changes make the JSON match Habbo's: `documentClass` is added (the bundle name), and effect and
figure libraries drop `name`, which Habbo's JSON for those does not have. Entries compressed with
zlib or gzip are both read.

Checked against SWF conversions of the same assets with `--profile sulake`: JSON identical and
every frame pixel-identical (furni, pets, effects, figure parts). All 19,959 bundles of a full
nitro-react asset set convert and validate, in about 20 seconds.

What a `.nitro` cannot give you:

- **Only what nitro-converter kept**, which matches the `sulake` profile: no 32px art, no `sh_`
  figure parts, no raw XML/binary symbols, and no separate pet palette entries. Converting the
  original `.swf` with the `full` profile keeps all of that. `--profile`, `--layout` and
  `--max-atlas` do not apply to `.nitro` input.
- **Older "generic" bundles** (`tile_cursor`, `place_holder` and similar) use nitro-converter's
  own schema (`type`, `dimensions`, `directions`). They are repackaged unchanged and the report
  carries a warning; Habbo's own files for those use a different layout.
- **The artwork is whatever the bundle was built from.** If a `.nitro` came from a different
  release of an asset than your SWFs, the pixels differ accordingly.

## Back to .swf and .nitro

`export` turns `.hab` bundles back into the two older formats. It also takes `.nitro` and `.swf`
input, converting it to a `.hab` in memory first, so `.nitro` -> `.swf` and `.swf` -> `.nitro` work
as well:

```
python -m swf2hab export hab/dcr/hof_furni -o dcr/hof_furni --to swf --report swf.json
python -m swf2hab export nitro-react/dist/bundled -o swf/ --to swf
python -m swf2hab export hab/ -o nitro/ --to nitro
```

### .swf

The SWF is built the way Habbo's own libraries are, so Habbo's AS3 client (Flash or AIR) loads it
like any other library. Its document class has one static `Class` property per asset, and each
property is bound to a bitmap, binary (XML) or sound tag. The ActionScript bytecode is generated
directly, so no Flex or AIR SDK is needed. The classes extend the player's own `Bitmap`,
`ByteArray` and `Sound` rather than Flex's `mx.core` wrappers, which the client never uses. That
makes the files about half the size of Habbo's.

- **XML.** A `--profile full` bundle carries the library's own XML, which is used as is.
  Otherwise the XML is regenerated from the JSON. Regeneration is the exact inverse of the mapping
  `convert` uses, so the regenerated library converts back to the same JSON.
- **Bitmaps** are cut out of the atlas at their original size, with the trimmed border put back.
  They are stored as premultiplied 32-bit bitmaps, as Habbo stores them. Pixels that came from a
  Habbo SWF come back bit-identical.
- **Shared bitmaps** (one image under several asset names) are shared again.
- **32px art.** Habbo's `.hab` files, the `sulake` profile and `.nitro` bundles have no 32px furni
  art. The AS3 client then draws 64px art at double size in a zoomed-out room. `--small auto`
  (the default) adds a 32px visualization with half-size art and halved offsets. Habbo's own 32px
  art is drawn by hand, so this is an approximation. `--small none` leaves it out.

Checked by converting each library to `.hab`, exporting it to `.swf` and converting that again,
with both profiles, on 18,588 furni SWFs and 3,856 other Habbo libraries (figure parts, effects,
pets, room and game content):

- **`full` profile:** the JSON was identical and every frame pixel-identical for every library
  except one corrupt SWF. Bitmaps Habbo stored as JPEG count as identical once premultiplied,
  which is how Flash stores and draws them.
- **`sulake` profile:** the same, except 18 libraries whose JSON has an empty `assets` or
  `aliases` object (every asset was 32px or `sh_` art, or the library had no art). XML cannot
  express an empty object, so it does not come back empty. Their frames were identical.
- **`.nitro`:** of all 19,959 bundles of a nitro-react asset set, 99.3% came back identical. The
  rest are details a SWF cannot hold, such as nitro-converter's legacy schema, or `sh_` art a
  `.nitro` never had.

Loaded in Adobe AIR's runtime with a harness that does what Habbo's `LibraryLoader` does, the
exports behaved the same as Habbo's originals:

- **68 of 69** Habbo originals and their exports gave identical results for every asset class:
  bitmap size, pixel checksum and XML bytes. The 69th has bitmaps whose stored colour exceeds
  their alpha, which no decoded image can keep.
- **124 of 124** exports made from `sulake` bundles and `.nitro` files (furni with generated 32px
  art, effects, figure parts and pets) loaded with every asset resolving.

`tools/air/` has the harness, so you can repeat this check on your own files.

### .nitro

The bundle gets the JSON and atlas nitro-converter would write:

- The JSON loses `documentClass`. Effects and figure libraries get their `name` back.
- 32px art is left out, as nitro-converter does.
- The atlas PNG is copied unchanged when every frame is on it. Otherwise the frames are repacked
  into one atlas.

`.nitro` -> `.hab` -> `.nitro` gives back the same JSON and byte-identical PNG for 19,949 of
19,959 bundles. The remaining 10 have an empty name. From `--profile full` bundles, 1,345 of
1,454 furni matched nitro-converter's own `.nitro` for the same item exactly (JSON and frame
list). Most of the others have one extra frame for a bitmap no asset uses.

Library-layout bundles (room content, UI libraries) have no Nitro equivalent and are skipped.

## The .hab format

```
0   "HAB\0"
4   u16 version = 1        6  u16 flags = 1
8   u32 index zlib length  12 u32 index JSON length   16 u32 payload length
20  zlib(index JSON) + payload
```

The index is `{"format":"hab","version":1,"name":...,"entries":[{name, mimeType, offset,
storedLength, originalLength, compression:"deflate"|"none"}]}`, plus optional `manifest`,
`aliases` and `generator`. An entry is stored deflated only if that makes it smaller.

In the atlas layout the JSON uses the Nitro/Habbo asset schema: `name`, `logicType`,
`visualizationType`, `assets`, `aliases`, `palettes`, `animations`, `logic`, `visualizations`,
`documentClass` and `spritesheet`. The spritesheet is in Pixi format, with frames keyed by the
SWF symbol class name and trimmed frames described by `spriteSourceSize`. A frame stored
outside the main atlas carries an extra `"image"` key, and `meta.images` lists every atlas.

## What does not convert

- **Code-only SWFs** such as `Habbo.swf` and `apiplayer.swf`. They are skipped, or you can
  exclude them.
- **Vector shapes, timelines, fonts and text fields.** A `.hab` only carries bitmaps, binary
  data and sounds. The report counts these tags so you can keep the SWF for those files.
- **JPEG bitmaps** are decoded and stored losslessly in the atlas. That needs Pillow, and a
  JPEG-heavy SWF gets bigger (game rooms built from photos, for example). Without Pillow, JPEGs
  are kept as separate `image/jpeg` entries.

## Benchmark

`tools/bench/` has a browser benchmark that compares decoding SWF and HAB on a random sample of
your own corpus:

```
python tools/bench/make_sample.py report.json -o bench
python -m http.server 8799 -d bench
```

Then open `http://127.0.0.1:8799/bench.html`.

## Credits and license

GPL-3.0-or-later (see `LICENSE`). The XML-to-JSON mapping rules follow
[nitro-converter](https://git.krews.org/nitro/nitro-converter) (GPL-3.0). The SWF library layout
and the generated 32px art follow [nitro-swf-converter](https://github.com/NextGenHabbo/nitro-swf-converter)
(MIT). See `NOTICE.md`.
Habbo, its assets and the `.hab` format belong to Sulake/Azerion. This tool contains none of
their assets.
