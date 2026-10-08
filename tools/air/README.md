# Checking exported SWFs in Adobe AIR

`Harness.as` loads asset-library SWFs the way Habbo's AS3 `LibraryLoader` and `AssetLibrary` do.
It reads `getDefinition(<library>).manifest`, resolves every manifest asset on the document class,
and instantiates every static `Class` property. For each SWF it writes one JSON line with each
asset's type, bitmap size and an Adler-32 checksum of its pixels or bytes. Running it on a Habbo
SWF and on the export of that SWF's `.hab` shows whether the client gets the same assets.

You need the Harman AIR SDK (https://airsdk.harman.com) and Java.

```
cd tools/air
<AIR SDK>/bin/amxmlc -output Harness.swf Harness.as
<AIR SDK>/bin/adl harness.xml -- results.jsonl C:/path/original/throne.swf C:/path/exported/throne.swf
```

The harness finds the document class from the file name, so keep each SWF named after its
library. Use absolute paths. `adl` opens one runtime per call and exits when the list is done.
