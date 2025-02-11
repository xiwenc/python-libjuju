import argparse
import builtins
import functools
import json
import keyword
import pprint
import re
import textwrap
import typing
from collections import defaultdict
from glob import glob
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeVar, Union

from . import codegen

_marker = object()

JUJU_VERSION = re.compile(r'[0-9]+\.[0-9-]+[\.\-][0-9a-z]+(\.[0-9]+)?')
# Workaround for https://bugs.launchpad.net/juju/+bug/1683906
NAUGHTY_CLASSES = ['ClientFacade', 'Client', 'FullStatus', 'ModelStatusInfo',
                   'ModelInfo', 'ApplicationDeploy']


# Map basic types to Python's typing with a callable
SCHEMA_TO_PYTHON = {
    'string': str,
    'integer': int,
    'float': float,
    'number': float,
    'boolean': bool,
    'object': Any,
}


# Friendly warning message to stick at the top of generated files.
HEADER = """\
# DO NOT CHANGE THIS FILE! This file is auto-generated by facade.py.
# Changes will be overwritten/lost when the file is regenerated.

"""


# Classes and helper functions that we'll write to _client.py
LOOKUP_FACADE = '''
def lookup_facade(name, version):
    """
    Given a facade name and version, attempt to pull that facade out
    of the correct client<version>.py file.

    """
    for _version in range(int(version), 0, -1):
        try:
            facade = getattr(CLIENTS[str(_version)], name)
            return facade
        except (KeyError, AttributeError):
            continue
    else:
        raise ImportError("No supported version for facade: "
                          "{}".format(name))


'''

TYPE_FACTORY = '''
class TypeFactory:
    @classmethod
    def from_connection(cls, connection):
        """
        Given a connected Connection object, return an initialized and
        connected instance of an API Interface matching the name of
        this class.

        @param connection: initialized Connection object.

        """
        facade_name = cls.__name__
        if not facade_name.endswith('Facade'):
           raise TypeError('Unexpected class name: {}'.format(facade_name))
        facade_name = facade_name[:-len('Facade')]
        version = connection.facades.get(facade_name)
        if version is None:
            raise Exception('No facade {} in facades {}'.format(facade_name,
                                                                connection.facades))

        c = lookup_facade(cls.__name__, version)
        c = c()
        c.connect(connection)

        return c


'''

CLIENT_TABLE = '''
CLIENTS = {{
    {clients}
}}


'''


class KindRegistry(dict):
    def register(self, name, version, obj):
        self[name] = {version: {
            "object": obj,
        }}

    def lookup(self, name, version=None):
        """If version is omitted, max version is used"""
        versions = self.get(name)
        if not versions:
            return None
        if version:
            return versions[version]
        return versions[max(versions)]

    def getObj(self, name, version=None):
        result = self.lookup(name, version)
        if result:
            obj = result["object"]
            return obj
        return None


class TypeRegistry(dict):
    def get(self, name):
        # Two way mapping
        refname = Schema.referenceName(name)
        if refname not in self:
            result = TypeVar(refname)
            self[refname] = result
            self[result] = refname

        return self[refname]


_types = TypeRegistry()
_registry = KindRegistry()
CLASSES = {}
factories = codegen.Capture()


def booler(v):
    if isinstance(v, str):
        if v == "false":
            return False
    return bool(v)


def getRefType(ref):
    return _types.get(ref)


def refType(obj):
    return getRefType(obj["$ref"])


def objType(obj):
    kind = obj.get('type')
    if not kind:
        raise ValueError("%s has no type" % obj)
    result = SCHEMA_TO_PYTHON.get(kind)
    if not result:
        raise ValueError("%s has type %s" % (obj, kind))
    return result


basic_types = [str, bool, int, float]


def name_to_py(name):
    result = name.replace("-", "_")
    result = result.lower()
    if keyword.iskeyword(result) or result in dir(builtins):
        result += "_"
    return result


def strcast(kind, keep_builtins=False):
    if (kind in basic_types or
            type(kind) in basic_types) and keep_builtins is False:
        return kind.__name__
    if str(kind).startswith('~'):
        return str(kind)[1:]
    if issubclass(kind, typing.GenericMeta):
        return str(kind)[1:]
    return kind


class Args(list):
    def __init__(self, defs):
        self.defs = defs
        if defs:
            rtypes = _registry.getObj(_types[defs])
            if len(rtypes) == 1:
                if not self.do_explode(rtypes[0][1]):
                    for name, rtype in rtypes:
                        self.append((name, rtype))
            else:
                for name, rtype in rtypes:
                    self.append((name, rtype))

    def do_explode(self, kind):
        if kind in basic_types or type(kind) is typing.TypeVar:
            return False
        if not issubclass(kind, (typing.Sequence,
                                 typing.Mapping)):
            self.clear()
            self.extend(Args(kind))
            return True
        return False

    def PyToSchemaMapping(self):
        m = {}
        for n, rt in self:
            m[name_to_py(n)] = n
        return m

    def SchemaToPyMapping(self):
        m = {}
        for n, tr in self:
            m[n] = name_to_py(n)
        return m

    def _format(self, name, rtype, typed=True):
        if typed:
            return "{} : {}".format(
                name_to_py(name),
                strcast(rtype)
            )
        else:
            return name_to_py(name)

    def _get_arg_str(self, typed=False, joined=", "):
        if self:
            parts = []
            for item in self:
                parts.append(self._format(item[0], item[1], typed))
            if joined:
                return joined.join(parts)
            return parts
        return ''

    def as_kwargs(self):
        if self:
            parts = []
            for item in self:
                parts.append('{}=None'.format(name_to_py(item[0])))
            return ', '.join(parts)
        return ''

    def typed(self):
        return self._get_arg_str(True)

    def __str__(self):
        return self._get_arg_str(False)

    def get_doc(self):
        return self._get_arg_str(True, "\n")


def buildTypes(schema, capture):
    INDENT = "    "
    for kind in sorted((k for k in _types if not isinstance(k, str)),
                       key=lambda x: str(x)):
        name = _types[kind]
        if name in capture and name not in NAUGHTY_CLASSES:
            continue
        args = Args(kind)
        # Write Factory class for _client.py
        make_factory(name)
        # Write actual class
        source = ["""
class {}(Type):
    _toSchema = {}
    _toPy = {}
    def __init__(self{}{}, **unknown_fields):
        '''
{}
        '''""".format(
            name,
            # pprint these to get stable ordering across regens
            pprint.pformat(args.PyToSchemaMapping(), width=999),
            pprint.pformat(args.SchemaToPyMapping(), width=999),
            ", " if args else "",
            args.as_kwargs(),
            textwrap.indent(args.get_doc(), INDENT * 2))]

        if not args:
            source.append("{}pass".format(INDENT * 2))
        else:
            for arg in args:
                arg_name = name_to_py(arg[0])
                arg_type = arg[1]
                arg_type_name = strcast(arg_type)
                if arg_type in basic_types:
                    source.append("{}self.{} = {}".format(INDENT * 2,
                                                          arg_name,
                                                          arg_name))
                elif type(arg_type) is typing.TypeVar:
                    source.append("{}self.{} = {}.from_json({}) "
                                  "if {} else None".format(INDENT * 2,
                                                           arg_name,
                                                           arg_type_name,
                                                           arg_name,
                                                           arg_name))
                elif issubclass(arg_type, typing.Sequence):
                    value_type = (
                        arg_type_name.__parameters__[0]
                        if len(arg_type_name.__parameters__)
                        else None
                    )
                    if type(value_type) is typing.TypeVar:
                        source.append(
                            "{}self.{} = [{}.from_json(o) "
                            "for o in {} or []]".format(INDENT * 2,
                                                        arg_name,
                                                        strcast(value_type),
                                                        arg_name))
                    else:
                        source.append("{}self.{} = {}".format(INDENT * 2,
                                                              arg_name,
                                                              arg_name))
                elif issubclass(arg_type, typing.Mapping):
                    value_type = (
                        arg_type_name.__parameters__[1]
                        if len(arg_type_name.__parameters__) > 1
                        else None
                    )
                    if type(value_type) is typing.TypeVar:
                        source.append(
                            "{}self.{} = {{k: {}.from_json(v) "
                            "for k, v in ({} or dict()).items()}}".format(
                                INDENT * 2,
                                arg_name,
                                strcast(value_type),
                                arg_name))
                    else:
                        source.append("{}self.{} = {}".format(INDENT * 2,
                                                              arg_name,
                                                              arg_name))
                else:
                    source.append("{}self.{} = {}".format(INDENT * 2,
                                                          arg_name,
                                                          arg_name))
            # Ensure that we take the kwargs (unknown_fields) and put it on the
            # Results/Params so we can inspect it.
            source.append("{}self.unknown_fields = unknown_fields".format(INDENT * 2))

        source = "\n".join(source)
        capture.clear(name)
        capture[name].write(source)
        capture[name].write("\n\n")
        co = compile(source, __name__, "exec")
        ns = _getns()
        exec(co, ns)
        cls = ns[name]
        CLASSES[name] = cls


def retspec(defs):
    # return specs
    # only return 1, so if there is more than one type
    # we need to include a union
    # In truth there is only 1 return
    # Error or the expected Type
    if not defs:
        return None
    if defs in basic_types:
        return strcast(defs, False)
    rtypes = _registry.getObj(_types[defs])
    if not rtypes:
        return None
    if len(rtypes) > 1:
        return Union[tuple([strcast(r[1], True) for r in rtypes])]
    return strcast(rtypes[0][1], False)


def return_type(defs):
    if not defs:
        return None
    rtypes = _registry.getObj(_types[defs])
    if not rtypes:
        return None
    if len(rtypes) > 1:
        for n, t in rtypes:
            if n == "Error":
                continue
            return t
    return rtypes[0][1]


def type_anno_func(func, defs, is_result=False):
    annos = {}
    if not defs:
        return func
    rtypes = _registry.getObj(_types[defs])
    if is_result:
        kn = "return"
        if not rtypes:
            annos[kn] = None
        elif len(rtypes) > 1:
            annos[kn] = Union[tuple([r[1] for r in rtypes])]
        else:
            annos[kn] = rtypes[0][1]
    else:
        for name, rtype in rtypes:
            name = name_to_py(name)
            annos[name] = rtype
    func.__annotations__.update(annos)
    return func


def ReturnMapping(cls):
    # Annotate the method with a return Type
    # so the value can be cast
    def decorator(f):
        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            nonlocal cls
            reply = await f(*args, **kwargs)
            if cls is None:
                return reply
            if 'error' in reply:
                cls = CLASSES['Error']
            if issubclass(cls, typing.Sequence):
                result = []
                item_cls = cls.__parameters__[0]
                for item in reply:
                    result.append(item_cls.from_json(item))
                    """
                    if 'error' in item:
                        cls = CLASSES['Error']
                    else:
                        cls = item_cls
                    result.append(cls.from_json(item))
                    """
            else:
                result = cls.from_json(reply['response'])

            return result
        return wrapper
    return decorator


def makeFunc(cls, name, params, result, _async=True):
    INDENT = "    "
    args = Args(params)
    assignments = []
    toschema = args.PyToSchemaMapping()
    for arg in args._get_arg_str(False, False):
        assignments.append("{}_params[\'{}\'] = {}".format(INDENT,
                                                           toschema[arg],
                                                           arg))
    assignments = "\n".join(assignments)
    res = retspec(result)
    source = """

@ReturnMapping({rettype})
{_async}def {name}(self{argsep}{args}):
    '''
{docstring}
    Returns -> {res}
    '''
    # map input types to rpc msg
    _params = dict()
    msg = dict(type='{cls.name}',
               request='{name}',
               version={cls.version},
               params=_params)
{assignments}
    reply = {_await}self.rpc(msg)
    return reply

"""

    fsource = source.format(_async="async " if _async else "",
                            name=name,
                            argsep=", " if args else "",
                            args=args,
                            res=res,
                            rettype=result.__name__ if result else None,
                            docstring=textwrap.indent(args.get_doc(), INDENT),
                            cls=cls,
                            assignments=assignments,
                            _await="await " if _async else "")
    ns = _getns()
    exec(fsource, ns)
    func = ns[name]
    return func, fsource


def buildMethods(cls, capture):
    properties = cls.schema['properties']
    for methodname in sorted(properties):
        method, source = _buildMethod(cls, methodname)
        setattr(cls, methodname, method)
        capture["{}Facade".format(cls.__name__)].write(source, depth=1)


def _buildMethod(cls, name):
    params = None
    result = None
    method = cls.schema['properties'][name]
    if 'properties' in method:
        prop = method['properties']
        spec = prop.get('Params')
        if spec:
            params = _types.get(spec['$ref'])
        spec = prop.get('Result')
        if spec:
            if '$ref' in spec:
                result = _types.get(spec['$ref'])
            else:
                result = SCHEMA_TO_PYTHON[spec['type']]
    return makeFunc(cls, name, params, result)


def buildFacade(schema):
    cls = type(schema.name, (Type,), dict(name=schema.name,
                                          version=schema.version,
                                          schema=schema))
    source = """
class {name}Facade(Type):
    name = '{name}'
    version = {version}
    schema = {schema}
    """.format(name=schema.name,
               version=schema.version,
               schema=textwrap.indent(pprint.pformat(schema), "    "))
    return cls, source


class TypeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Type):
            return obj.serialize()
        return json.JSONEncoder.default(self, obj)


class Type:
    def connect(self, connection):
        self.connection = connection

    async def rpc(self, msg):
        result = await self.connection.rpc(msg, encoder=TypeEncoder)
        return result

    @classmethod
    def from_json(cls, data):
        if isinstance(data, cls):
            return data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                raise
        d = {}
        for k, v in (data or {}).items():
            d[cls._toPy.get(k, k)] = v

        try:
            return cls(**d)
        except TypeError:
            raise

    def serialize(self):
        d = {}
        for attr, tgt in self._toSchema.items():
            d[tgt] = getattr(self, attr)
        return d

    def to_json(self):
        return json.dumps(self.serialize(), cls=TypeEncoder, sort_keys=True)


class Schema(dict):
    def __init__(self, schema):
        self.name = schema['Name']
        self.version = schema['Version']
        self.update(schema['Schema'])

    @classmethod
    def referenceName(cls, ref):
        if ref.startswith("#/definitions/"):
            ref = ref.rsplit("/", 1)[-1]
        return ref

    def resolveDefinition(self, ref):
        return self['definitions'][self.referenceName(ref)]

    def deref(self, prop, name):
        if not isinstance(prop, dict):
            raise TypeError(prop)
        if "$ref" not in prop:
            return prop

        target = self.resolveDefinition(prop["$ref"])
        return target

    def buildDefinitions(self):
        # here we are building the types out
        # anything in definitions is a type
        # but these may contain references themselves
        # so we dfs to the bottom and build upwards
        # when a types is already in the registry
        defs = self.get('definitions')
        if not defs:
            return
        for d, data in defs.items():
            if d in _registry and d not in NAUGHTY_CLASSES:
                continue
            node = self.deref(data, d)
            kind = node.get("type")
            if kind == "object":
                result = self.buildObject(node, d)
            elif kind == "array":
                pass
            _registry.register(d, self.version, result)
            # XXX: This makes sure that the type gets added to the global
            # _types dict even if no other type in the schema has a ref
            # to it.
            getRefType(d)

    def buildObject(self, node, name=None, d=0):
        # we don't need to build types recursively here
        # they are all in definitions already
        # we only want to include the type reference
        # which we can derive from the name
        struct = []
        add = struct.append
        props = node.get("properties")
        pprops = node.get("patternProperties")
        if props:
            # Sort these so the __init__ arg list for each Type remains
            # consistently ordered across regens of client.py
            for p in sorted(props):
                prop = props[p]
                if "$ref" in prop:
                    add((p, refType(prop)))
                else:
                    kind = prop['type']
                    if kind == "array":
                        add((p, self.buildArray(prop, d + 1)))
                    elif kind == "object":
                        struct.extend(self.buildObject(prop, p, d + 1))
                    else:
                        add((p, objType(prop)))
        if pprops:
            if ".*" not in pprops:
                raise ValueError(
                    "Cannot handle actual pattern in patternProperties %s" %
                    pprops)
            pprop = pprops[".*"]
            if "$ref" in pprop:
                add((name, Mapping[str, refType(pprop)]))
                return struct
            ppkind = pprop["type"]
            if ppkind == "array":
                add((name, self.buildArray(pprop, d + 1)))
            else:
                add((name, Mapping[str, SCHEMA_TO_PYTHON[ppkind]]))

        if not struct and node.get('additionalProperties', False):
            add((name, Mapping[str, SCHEMA_TO_PYTHON['object']]))

        return struct

    def buildArray(self, obj, d=0):
        # return a sequence from an array in the schema
        if "$ref" in obj:
            return Sequence[refType(obj)]
        else:
            kind = obj.get("type")
            if kind and kind == "array":
                items = obj['items']
                return self.buildArray(items, d + 1)
            else:
                return Sequence[objType(obj)]


def _getns():
    ns = {'Type': Type,
          'typing': typing,
          'ReturnMapping': ReturnMapping
          }
    # Copy our types into the globals of the method
    for facade in _registry:
        ns[facade] = _registry.getObj(facade)
    return ns


def make_factory(name):
    if name in factories:
        del factories[name]
    factories[name].write("class {}(TypeFactory):\n    pass\n\n".format(name))


def write_facades(captures, options):
    """
    Write the Facades to the appropriate _client<version>.py

    """
    for version in sorted(captures.keys()):
        filename = "{}/_client{}.py".format(options.output_dir, version)
        with open(filename, "w") as f:
            f.write(HEADER)
            f.write("from juju.client.facade import Type, ReturnMapping\n")
            f.write("from juju.client._definitions import *\n\n")
            for key in sorted(
                    [k for k in captures[version].keys() if "Facade" in k]):
                print(captures[version][key], file=f)

    # Return the last (most recent) version for use in other routines.
    return version


def write_definitions(captures, options, version):
    """
    Write auxillary (non versioned) classes to
    _definitions.py The auxillary classes currently get
    written redudantly into each capture object, so we can look in
    one of them -- we just use the last one from the loop above.

    """
    with open("{}/_definitions.py".format(options.output_dir), "w") as f:
        f.write(HEADER)
        f.write("from juju.client.facade import Type, ReturnMapping\n\n")
        for key in sorted(
                [k for k in captures[version].keys() if "Facade" not in k]):
            print(captures[version][key], file=f)


def write_client(captures, options):
    """
    Write the TypeFactory classes to _client.py, along with some
    imports and tables so that we can look up versioned Facades.

    """
    with open("{}/_client.py".format(options.output_dir), "w") as f:
        f.write(HEADER)
        f.write("from juju.client._definitions import *\n\n")
        clients = ", ".join("_client{}".format(v) for v in captures)
        f.write("from juju.client import " + clients + "\n\n")
        f.write(CLIENT_TABLE.format(clients=",\n    ".join(
            ['"{}": _client{}'.format(v, v) for v in captures])))
        f.write(LOOKUP_FACADE)
        f.write(TYPE_FACTORY)
        for key in sorted([k for k in factories.keys() if "Facade" in k]):
            print(factories[key], file=f)


def generate_facades(options):
    captures = defaultdict(codegen.Capture)
    schemas = {}
    for p in sorted(glob(options.schema)):
        if 'latest' in p:
            juju_version = 'latest'
        else:
            try:
                juju_version = re.search(JUJU_VERSION, p).group()
            except AttributeError:
                print("Cannot extract a juju version from {}".format(p))
                print("Schemas must include a juju version in the filename")
                raise SystemExit(1)

        new_schemas = json.loads(Path(p).read_text("utf-8"))
        schemas[juju_version] = [Schema(s) for s in new_schemas]

    # Build all of the auxillary (unversioned) classes
    # TODO: get rid of some of the excess trips through loops in the
    # called functions.
    for juju_version in sorted(schemas.keys()):
        for schema in schemas[juju_version]:
            schema.buildDefinitions()
            buildTypes(schema, captures[schema.version])

    # Build the Facade classes
    for juju_version in sorted(schemas.keys()):
        for schema in schemas[juju_version]:
            cls, source = buildFacade(schema)
            cls_name = "{}Facade".format(schema.name)

            captures[schema.version].clear(cls_name)
            # Make the factory class for _client.py
            make_factory(cls_name)
            # Make the actual class
            captures[schema.version][cls_name].write(source)
            # Build the methods for each Facade class.
            buildMethods(cls, captures[schema.version])
            # Mark this Facade class as being done for this version --
            # helps mitigate some excessive looping.
            CLASSES[schema.name] = cls

    return captures


def setup():
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--schema", default="juju/client/schemas*")
    parser.add_argument("-o", "--output_dir", default="juju/client")
    options = parser.parse_args()
    return options


def main():
    options = setup()

    # Generate some text blobs
    captures = generate_facades(options)

    # ... and write them out
    last_version = write_facades(captures, options)
    write_definitions(captures, options, last_version)
    write_client(captures, options)


if __name__ == '__main__':
    main()
