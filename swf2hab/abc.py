"""ActionScript 3 bytecode (ABC) for asset-library SWFs: a writer, and a reader for checking.

A Habbo asset library is a SWF whose document class exposes every asset as a static `Class`
property. The client's loader reads `getDefinition(<library>).manifest`, instantiates it as a
ByteArray holding the manifest XML, then fetches each asset the manifest lists as
`<document class>[<asset name>]` and instantiates that class: a Bitmap subclass for images, a
ByteArray subclass for XML/binary data, a Sound subclass for sounds. The Flash runtime fills those
instances from the DefineBitsLossless2 / DefineBinaryData / DefineSound tag that SymbolClass binds
to each class name, so the classes carry no code beyond calling their superclass constructor.

`library_abc` emits exactly that: one class per asset, then the document class, whose static
initialiser stores each asset class in a static constant named after the asset. Flex's
`[Embed]` produces the same shape through mx.core.BitmapAsset / ByteArrayAsset / SoundAsset;
those add nothing the client uses, so the classes here extend the player's own Bitmap, ByteArray
and Sound directly and no framework code is linked.

The format is documented in Adobe's "ActionScript Virtual Machine 2 (AVM2) Overview".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# namespace kinds
PACKAGE_NS = 0x16
# multiname kinds
QNAME = 0x07
# trait kinds
TRAIT_SLOT, TRAIT_CONST, TRAIT_CLASS = 0, 6, 4
# instance flags
CLASS_SEALED = 0x01

# Superclass chains, root first, as the scope chain is built when a class is created.
CHAINS = {
    "bitmap": [("", "Object"), ("flash.events", "EventDispatcher"), ("flash.display", "DisplayObject"),
               ("flash.display", "Bitmap")],
    "binary": [("", "Object"), ("flash.utils", "ByteArray")],
    "sound": [("", "Object"), ("flash.events", "EventDispatcher"), ("flash.media", "Sound")],
    "sprite": [("", "Object"), ("flash.events", "EventDispatcher"), ("flash.display", "DisplayObject"),
               ("flash.display", "InteractiveObject"), ("flash.display", "DisplayObjectContainer"),
               ("flash.display", "Sprite")],
}

# opcodes used by the generated methods
OP_GETLOCAL0, OP_PUSHSCOPE, OP_POPSCOPE, OP_RETURNVOID = 0xD0, 0x30, 0x1D, 0x47
OP_CONSTRUCTSUPER, OP_GETLEX, OP_GETSCOPEOBJECT, OP_NEWCLASS, OP_INITPROPERTY = 0x49, 0x60, 0x65, 0x58, 0x68


def u30(n: int) -> bytes:
    if n < 0 or n >= 1 << 30:
        raise ValueError("u30 out of range: %d" % n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class _Pool:
    """Constant pool with interning. Index 0 of every table is the implicit 'any' entry."""

    def __init__(self):
        self.strings: list[str] = []
        self.namespaces: list[tuple[int, int]] = []
        self.multinames: list[tuple[int, int, int]] = []
        self._s: dict[str, int] = {}
        self._n: dict[tuple, int] = {}
        self._m: dict[tuple, int] = {}

    def string(self, s: str) -> int:
        if s not in self._s:
            self.strings.append(s)
            self._s[s] = len(self.strings)
        return self._s[s]

    def package(self, uri: str) -> int:
        key = (PACKAGE_NS, self.string(uri))
        if key not in self._n:
            self.namespaces.append(key)
            self._n[key] = len(self.namespaces)
        return self._n[key]

    def qname(self, package: str, name: str) -> int:
        key = (QNAME, self.package(package), self.string(name))
        if key not in self._m:
            self.multinames.append(key)
            self._m[key] = len(self.multinames)
        return self._m[key]

    def encode(self) -> bytes:
        out = bytearray()
        out += u30(0) + u30(0) + u30(0)          # no int, uint or double constants
        out += u30(len(self.strings) + 1)
        for s in self.strings:
            raw = s.encode("utf-8")
            out += u30(len(raw)) + raw
        out += u30(len(self.namespaces) + 1)
        for kind, name in self.namespaces:
            out.append(kind)
            out += u30(name)
        out += u30(0)                            # no namespace sets
        out += u30(len(self.multinames) + 1)
        for kind, ns, name in self.multinames:
            out.append(kind)
            out += u30(ns) + u30(name)
        return bytes(out)


@dataclass
class _Body:
    method: int
    max_stack: int
    local_count: int
    init_scope: int
    max_scope: int
    code: bytes


def library_abc(document_class: str, assets: list[tuple[str, str, str]]) -> bytes:
    """ABC for an asset library.

    `assets` is (property name, class name, kind) per asset, kind one of "bitmap", "binary" and
    "sound". The document class gets one static `Class` constant per property name.
    """
    pool = _Pool()
    methods = 0
    bodies: list[_Body] = []
    instances = bytearray()
    statics = bytearray()

    def new_method(code: bytes, max_stack: int, init_scope: int, max_scope: int) -> int:
        nonlocal methods
        index = methods
        methods += 1
        bodies.append(_Body(index, max_stack, 1, init_scope, max_scope, code))
        return index

    constructor = bytes([OP_GETLOCAL0, OP_PUSHSCOPE, OP_GETLOCAL0, OP_CONSTRUCTSUPER]) + u30(0) + bytes([OP_RETURNVOID])
    empty_cinit = bytes([OP_GETLOCAL0, OP_PUSHSCOPE, OP_RETURNVOID])
    class_type = pool.qname("", "Class")

    classes: list[tuple[str, str]] = [(cls, kind) for _, cls, kind in assets] + [(document_class, "sprite")]
    for index, (cls, kind) in enumerate(classes):
        chain = CHAINS[kind]
        outer = 1 + len(chain)           # global scope + the superclass chain
        name = pool.qname("", cls)
        iinit = new_method(constructor, 1, outer + 1, outer + 2)
        instances += u30(name) + u30(pool.qname(*chain[-1])) + bytes([CLASS_SEALED]) + u30(0) + u30(iinit)
        if index < len(assets):
            instances += u30(0)
            statics += u30(new_method(empty_cinit, 1, outer, outer + 1)) + u30(0)
            continue
        instances += u30(0)
        code = bytearray([OP_GETLOCAL0, OP_PUSHSCOPE])
        traits = bytearray()
        for slot, (prop, asset_cls, _) in enumerate(assets, 1):
            prop_name = pool.qname("", prop)
            code += bytes([OP_GETLOCAL0, OP_GETLEX]) + u30(pool.qname("", asset_cls))
            code += bytes([OP_INITPROPERTY]) + u30(prop_name)
            traits += u30(prop_name) + bytes([TRAIT_CONST]) + u30(slot) + u30(class_type) + u30(0)
        code.append(OP_RETURNVOID)
        statics += u30(new_method(bytes(code), 2, outer, outer + 1)) + u30(len(assets)) + traits

    # One script creates every class, asset classes first, so the document class's static
    # initialiser finds them all on the global object.
    deepest = max(len(CHAINS[kind]) for _, kind in classes)
    code = bytearray([OP_GETLOCAL0, OP_PUSHSCOPE])
    script_traits = bytearray()
    for index, (cls, kind) in enumerate(classes):
        chain = CHAINS[kind]
        code += bytes([OP_GETSCOPEOBJECT]) + u30(0)
        for package, base in chain:
            code += bytes([OP_GETLEX]) + u30(pool.qname(package, base)) + bytes([OP_PUSHSCOPE])
        code += bytes([OP_GETLEX]) + u30(pool.qname(*chain[-1]))
        code += bytes([OP_NEWCLASS]) + u30(index)
        code += bytes([OP_POPSCOPE]) * len(chain)
        code += bytes([OP_INITPROPERTY]) + u30(pool.qname("", cls))
        script_traits += u30(pool.qname("", cls)) + bytes([TRAIT_CLASS]) + u30(index + 1) + u30(index)
    code.append(OP_RETURNVOID)
    script_init = new_method(bytes(code), 2, 1, 2 + deepest)

    out = bytearray(struct.pack("<HH", 16, 46))
    out += pool.encode()
    out += u30(methods)
    for _ in range(methods):
        out += u30(0) + u30(0) + u30(0) + bytes([0])   # no params, any return type, no name, no flags
    out += u30(0)                                        # no metadata
    out += u30(len(classes)) + instances + statics
    out += u30(1) + u30(script_init) + u30(len(classes)) + script_traits
    out += u30(len(bodies))
    for b in bodies:
        out += u30(b.method) + u30(b.max_stack) + u30(b.local_count) + u30(b.init_scope) + u30(b.max_scope)
        out += u30(len(b.code)) + b.code + u30(0) + u30(0)
    return bytes(out)


# ---------------------------------------------------------------------------------------- reader

@dataclass
class ClassInfo:
    name: str
    super_name: str
    flags: int
    instance_traits: list[tuple[str, int]] = field(default_factory=list)   # (name, kind)
    static_traits: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class AbcSummary:
    classes: list[ClassInfo]
    scripts: int
    methods: int


class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def u30(self) -> int:
        v = shift = 0
        for _ in range(5):
            b = self.u8()
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        return v

    def skip(self, n: int) -> None:
        self.p += n


def read_abc(data: bytes) -> AbcSummary:
    """Class names, superclasses and trait names of an ABC block (enough to check a library)."""
    r = _Reader(data)
    minor, major = struct.unpack_from("<HH", data, 0)
    r.skip(4)
    for _ in range(2):                       # ints, uints: variable-length
        for _ in range(max(0, r.u30() - 1)):
            r.u30()
    r.skip(8 * max(0, r.u30() - 1))         # doubles
    if (major, minor) >= (47, 16):          # ASC 2.0 / AIR SDK output adds a float pool
        r.skip(4 * max(0, r.u30() - 1))
    strings = [""]
    for _ in range(max(0, r.u30() - 1)):
        n = r.u30()
        strings.append(data[r.p:r.p + n].decode("utf-8", "replace"))
        r.skip(n)
    namespaces = [""]
    for _ in range(max(0, r.u30() - 1)):
        r.u8()
        namespaces.append(strings[r.u30()])
    for _ in range(max(0, r.u30() - 1)):    # namespace sets
        for _ in range(r.u30()):
            r.u30()
    multinames = ["*"]
    for _ in range(max(0, r.u30() - 1)):
        kind = r.u8()
        if kind in (0x07, 0x0D):
            ns, name = r.u30(), r.u30()
            multinames.append((namespaces[ns] + "::" if namespaces[ns] else "") + strings[name])
        elif kind in (0x0F, 0x10):
            multinames.append(strings[r.u30()])
        elif kind in (0x11, 0x12):
            multinames.append("*")
        elif kind in (0x09, 0x0E):
            name = r.u30()
            r.u30()
            multinames.append(strings[name])
        elif kind in (0x1B, 0x1C):
            r.u30()
            multinames.append("*")
        elif kind == 0x1D:
            base = r.u30()
            for _ in range(r.u30()):
                r.u30()
            multinames.append(multinames[base] if base < len(multinames) else "?")
        else:
            raise ValueError("unknown multiname kind 0x%02x" % kind)
    method_count = r.u30()
    for _ in range(method_count):
        params = r.u30()
        r.u30()
        for _ in range(params):
            r.u30()
        r.u30()
        flags = r.u8()
        if flags & 0x08:                    # HAS_OPTIONAL
            for _ in range(r.u30()):
                r.u30()
                r.u8()
        if flags & 0x80:                    # HAS_PARAM_NAMES
            for _ in range(params):
                r.u30()
    for _ in range(r.u30()):                # metadata
        r.u30()
        n = r.u30()
        for _ in range(2 * n):
            r.u30()

    def traits() -> list[tuple[str, int]]:
        out = []
        for _ in range(r.u30()):
            name = multinames[r.u30()]
            kind = r.u8()
            k = kind & 0x0F
            if k in (TRAIT_SLOT, TRAIT_CONST):
                r.u30()
                r.u30()
                if r.u30():
                    r.u8()
            else:
                r.u30()
                r.u30()
            if kind & 0x40:
                for _ in range(r.u30()):
                    r.u30()
            out.append((name, k))
        return out

    count = r.u30()
    classes = []
    for _ in range(count):
        name, super_name = multinames[r.u30()], multinames[r.u30()]
        flags = r.u8()
        if flags & 0x08:
            r.u30()
        for _ in range(r.u30()):
            r.u30()
        r.u30()
        classes.append(ClassInfo(name, super_name, flags, traits()))
    for c in classes:
        r.u30()
        c.static_traits = traits()
    scripts = r.u30()
    return AbcSummary(classes, scripts, method_count)
