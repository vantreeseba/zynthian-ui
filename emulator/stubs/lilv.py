"""Minimal stub of the lilv LV2 bindings: empty plugin world for desktop UI testing."""

LILV_URI_CONTROL_PORT = "http://lv2plug.in/ns/lv2core#ControlPort"
LILV_URI_INPUT_PORT = "http://lv2plug.in/ns/lv2core#InputPort"
LILV_URI_OUTPUT_PORT = "http://lv2plug.in/ns/lv2core#OutputPort"
LILV_URI_AUDIO_PORT = "http://lv2plug.in/ns/lv2core#AudioPort"
LILV_URI_MIDI_EVENT = "http://lv2plug.in/ns/ext/midi#MidiEvent"
LILV_URI_ATOM_PORT = "http://lv2plug.in/ns/ext/atom#AtomPort"
OPTION_FILTER_LANG = "http://drobilla.net/ns/lilv#filter-lang"


class _NamespaceAttr(str):
    def __getattr__(self, name):
        return _NamespaceAttr(str(self) + name)


class Namespace:
    def __init__(self, world, uri):
        self._uri = uri

    def __getattr__(self, name):
        return _NamespaceAttr(self._uri + name)


class _Namespaces:
    pass


class _Plugins:
    def __getitem__(self, uri):
        raise KeyError(uri)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class World:
    def __init__(self, *args, **kwargs):
        self.ns = _Namespaces()

    def load_all(self):
        pass

    def set_option(self, *args, **kwargs):
        pass

    def new_bool(self, v):
        return v

    def new_uri(self, uri):
        return uri

    def get_all_plugins(self):
        return _Plugins()

    def load_bundle(self, *args):
        pass

    def load_specifications(self):
        pass

    def load_plugin_classes(self):
        pass
