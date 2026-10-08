import haxe.io.Bytes;
import haxe.io.BytesInput;
import haxe.zip.InflateImpl;

/** Same routine as SkyHaxe's SwfFile.inflate, exported for the benchmark page. */
@:expose("SkyInflate")
class SkyInflate {
    public static function inflate(u8:js.lib.Uint8Array):js.lib.Uint8Array {
        var compressed = Bytes.ofData(u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength));
        var impl = new InflateImpl(new BytesInput(compressed), true, true);
        var buffer = Bytes.alloc(65536);
        var out = new haxe.io.BytesBuffer();
        while (true) {
            var read = impl.readBytes(buffer, 0, buffer.length);
            out.addBytes(buffer, 0, read);
            if (read < buffer.length) break;
        }
        var b = out.getBytes();
        return new js.lib.Uint8Array(b.getData(), 0, b.length);
    }
    static function main() {}
}
