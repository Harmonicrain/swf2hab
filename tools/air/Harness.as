package
{
    import flash.desktop.NativeApplication;
    import flash.display.Bitmap;
    import flash.display.BitmapData;
    import flash.display.Loader;
    import flash.display.Sprite;
    import flash.events.ErrorEvent;
    import flash.events.Event;
    import flash.events.IOErrorEvent;
    import flash.events.InvokeEvent;
    import flash.events.UncaughtErrorEvent;
    import flash.filesystem.File;
    import flash.filesystem.FileMode;
    import flash.filesystem.FileStream;
    import flash.media.Sound;
    import flash.system.ApplicationDomain;
    import flash.system.LoaderContext;
    import flash.utils.ByteArray;
    import flash.utils.describeType;
    import flash.utils.getQualifiedClassName;
    import flash.utils.getQualifiedSuperclassName;

    // Loads asset-library SWFs the way Habbo's LibraryLoader / AssetLibrary do and writes one JSON
    // line per SWF: every static Class property instantiated, with pixel / byte checksums.
    // adl harness.xml -- <output.jsonl> <a.swf> [<b.swf> ...]
    public class Harness extends Sprite
    {
        private var queue:Array;
        private var out:FileStream;

        public function Harness()
        {
            NativeApplication.nativeApplication.addEventListener(InvokeEvent.INVOKE, onInvoke);
        }

        private function onInvoke(e:InvokeEvent):void
        {
            var args:Array = e.arguments;
            out = new FileStream();
            out.open(new File(args[0]), FileMode.WRITE);
            queue = args.slice(1);
            next();
        }

        private function next():void
        {
            if (queue.length == 0)
            {
                out.close();
                NativeApplication.nativeApplication.exit(0);
                return;
            }
            var path:String = queue.shift();
            var bytes:ByteArray = new ByteArray();
            var fs:FileStream = new FileStream();
            fs.open(new File(path), FileMode.READ);
            fs.readBytes(bytes);
            fs.close();
            var loader:Loader = new Loader();
            var ctx:LoaderContext = new LoaderContext(false, new ApplicationDomain());
            ctx.allowCodeImport = true;
            loader.contentLoaderInfo.addEventListener(Event.COMPLETE, function (ev:Event):void
            {
                inspect(path, loader);
                loader.unloadAndStop();
                next();
            });
            loader.contentLoaderInfo.addEventListener(IOErrorEvent.IO_ERROR, function (ev:ErrorEvent):void
            {
                line({file: path, error: "load: " + ev.text});
                next();
            });
            loader.uncaughtErrorEvents.addEventListener(UncaughtErrorEvent.UNCAUGHT_ERROR, function (ev:UncaughtErrorEvent):void
            {
                line({file: path, error: "uncaught: " + String(ev.error)});
            });
            try
            {
                loader.loadBytes(bytes, ctx);
            }
            catch (err:Error)
            {
                line({file: path, error: "loadBytes: " + err.toString()});
                next();
            }
        }

        private function line(o:Object):void
        {
            out.writeUTFBytes(JSON.stringify(o) + "\n");
        }

        private static function adler(b:ByteArray):String
        {
            var s1:uint = 1;
            var s2:uint = 0;
            b.position = 0;
            var n:int = b.length;
            for (var i:int = 0; i < n; i++)
            {
                s1 = (s1 + b[i]) % 65521;
                s2 = (s2 + s1) % 65521;
            }
            return ((s2 << 16) | s1).toString(16);
        }

        private function inspect(path:String, loader:Loader):void
        {
            var result:Object = {file: path};
            try
            {
                var name:String = new File(path).name.replace(/\.swf$/i, "");
                var domain:ApplicationDomain = loader.contentLoaderInfo.applicationDomain;
                result.root = getQualifiedClassName(loader.content);
                // LibraryLoader.prepareLibrary
                var cls:Class = domain.getDefinition(name) as Class;
                result.docSuper = getQualifiedSuperclassName(cls);
                if (cls.hasOwnProperty("manifest"))
                {
                    var manifestClass:Class = cls.manifest as Class;
                    var mb:ByteArray = new manifestClass() as ByteArray;
                    var manifest:XML = new XML(mb.readUTFBytes(mb.length));
                    var listed:Array = [];
                    for each (var a:XML in manifest..asset)
                    {
                        // AssetLibrary.fetchLibraryContents: resource[name]
                        var n:String = String(a.@name);
                        listed.push(n + (cls[n] is Class ? "" : "!missing"));
                    }
                    result.manifest = listed;
                }
                var props:Object = {};
                var type:XML = describeType(cls);
                var names:Array = [];
                for each (var p:XML in type.constant + type.variable)
                {
                    if (String(p.@type) == "Class")
                    {
                        names.push(String(p.@name));
                    }
                }
                names.sort();
                for each (var pn:String in names)
                {
                    var ac:Class = cls[pn] as Class;
                    if (ac == null)
                    {
                        props[pn] = "null";
                        continue;
                    }
                    var inst:Object = new ac();
                    if (inst is Bitmap)
                    {
                        var bd:BitmapData = Bitmap(inst).bitmapData;
                        props[pn] = bd == null ? "bitmap:null" : "bitmap:" + bd.width + "x" + bd.height + ":" + adler(bd.getPixels(bd.rect));
                    }
                    else if (inst is ByteArray)
                    {
                        props[pn] = "bytes:" + ByteArray(inst).length + ":" + adler(ByteArray(inst));
                    }
                    else if (inst is Sound)
                    {
                        props[pn] = "sound:" + Sound(inst).length.toFixed(0) + ":" + Sound(inst).bytesTotal;
                    }
                    else
                    {
                        props[pn] = "other:" + getQualifiedClassName(inst);
                    }
                }
                result.props = props;
            }
            catch (err:Error)
            {
                result.error = err.toString();
            }
            line(result);
        }
    }
}
