# swf2hab

Converts Habbo Flash asset libraries (`.swf`) into `.hab` bundles, the format Habbo's own HTML5
client loads instead of SWFs. That covers furni, pets, figure parts, effects and room content.

- **Standalone.** Python 3.9+ standard library only. Installing `numpy` and `Pillow` makes it
  faster and the PNGs smaller, but the decoded pixels are the same either way.
- **Checked against Habbo.** Run on Habbo's own `.swf` files, it reproduces the `.hab` files
  Habbo serves from `images.habbo.com`. On a test set of 75 libraries (61 furni plus figure,
  pet, effect and placeholder libraries), every JSON document matched and all 7,333 frames were
  pixel-identical, and the output was about 7% smaller. To repeat the check, see
  `tools/oracle_check.py`.
- **Safe.** It only reads your SWFs and writes into a separate output folder.

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
[nitro-converter](https://git.krews.org/nitro/nitro-converter) (GPL-3.0); see `NOTICE.md`.
Habbo, its assets and the `.hab` format belong to Sulake/Azerion. This tool contains none of
their assets.
