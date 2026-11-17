"""Tests for power_watchdog_presence.py — the advertisement-router client.

Everything here runs against fakes: the mode decision against a temporary
/service tree, the router client against a recording bus, the BlueZ name
lookup against a proxy that answers for chosen object paths only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import power_watchdog_presence as pp


# ── Mode decision ──────────────────────────────────────────────────────────


class TestRouterProviderInstalled:
    def test_nothing_installed(self, tmp_path):
        dirs = (str(tmp_path / "a"), str(tmp_path / "b"))
        assert pp.router_provider_installed(dirs) is None

    def test_service_symlink_counts(self, tmp_path):
        # The installer creates /service/<name> -> /data/apps/<name>/service.
        target = tmp_path / "data" / "apps" / "dbus-ble-sensors-py" / "service"
        target.mkdir(parents=True)
        link = tmp_path / "service" / "dbus-ble-sensors-py"
        link.parent.mkdir()
        link.symlink_to(target)
        dirs = (str(tmp_path / "service" / "missing"), str(link))
        assert pp.router_provider_installed(dirs) == str(link)

    def test_dangling_symlink_does_not_count(self, tmp_path):
        link = tmp_path / "dbus-ble-sensors-py"
        link.symlink_to(tmp_path / "gone")
        assert pp.router_provider_installed((str(link),)) is None

    def test_first_match_wins(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert pp.router_provider_installed((str(a), str(b))) == str(a)

    def test_known_providers(self):
        assert "/service/dbus-ble-sensors-py" in pp.ROUTER_PROVIDER_SERVICE_DIRS
        assert "/service/dbus-ble-advertisements" in pp.ROUTER_PROVIDER_SERVICE_DIRS


class TestResolvePresenceSource:
    @pytest.mark.parametrize("raw,installed,expected", [
        ("auto", "/service/x", "router"),
        ("auto", None, "scan"),
        (None, "/service/x", "router"),
        (None, None, "scan"),
        ("", None, "scan"),
        ("router", None, "router"),
        ("scan", "/service/x", "scan"),
        (" Router ", None, "router"),
        # A typo must not silently pick a mode: it falls back to auto.
        ("bogus", "/service/x", "router"),
        ("bogus", None, "scan"),
    ])
    def test_resolution(self, raw, installed, expected):
        assert pp.resolve_presence_source(raw, installed) == expected


# ── Registration paths ─────────────────────────────────────────────────────


class TestRegistrationPaths:
    def test_manufacturer_path_is_decimal(self):
        # Hughes' company ID is 0x2312; the router contract wants decimal.
        assert pp.HUGHES_MANUFACTURER_ID == 8978
        assert pp.mfgr_registration_path() == \
            "/ble_advertisements/power_watchdog/mfgr/8978"

    def test_addr_path_is_uppercase_no_separators(self):
        assert pp.addr_registration_path("aa:bb:cc:dd:ee:ff") == \
            "/ble_advertisements/power_watchdog/addr/AABBCCDDEEFF"

    def test_registration_paths_dedupe_spellings(self):
        paths = pp.registration_paths(["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff"])
        assert paths == [
            "/ble_advertisements/power_watchdog/mfgr/8978",
            "/ble_advertisements/power_watchdog/addr/AABBCCDDEEFF",
        ]

    def test_no_known_macs_still_registers_manufacturer(self):
        assert pp.registration_paths() == [pp.mfgr_registration_path()]

    def test_label_is_a_substring_of_our_bus_name(self):
        # The router uses label-in-bus-name as its cleanup hint.
        assert pp.SERVICE_LABEL in "com.victronenergy.switch.power_watchdog"


# ── BlueZ lookup ───────────────────────────────────────────────────────────


class TestBluezDevicePath:
    def test_colon_mac(self):
        assert pp.bluez_device_path("hci5", "24:EC:4A:E4:69:A5") == \
            "/org/bluez/hci5/dev_24_EC_4A_E4_69_A5"

    def test_lowercase_bare_mac(self):
        assert pp.bluez_device_path("hci0", "24ec4ae469a5") == \
            "/org/bluez/hci0/dev_24_EC_4A_E4_69_A5"


class TestPresentHciNames:
    def test_sorted_numerically_and_connections_excluded(self, tmp_path):
        for name in ("hci10", "hci2", "hci0", "hci3:77", "rfkill"):
            (tmp_path / name).mkdir()
        assert pp.present_hci_names(str(tmp_path)) == ["hci0", "hci2", "hci10"]

    def test_missing_sysfs(self, tmp_path):
        assert pp.present_hci_names(str(tmp_path / "nope")) == []


class _FakeProps:
    def __init__(self, names: dict[str, str], tried: list[str], path: str):
        self._names = names
        self._tried = tried
        self._path = path

    def Get(self, interface, prop):
        self._tried.append(self._path)
        if self._path not in self._names:
            raise Exception("UnknownObject")
        return self._names[self._path]


class _FakeBluezBus:
    def __init__(self, names: dict[str, str]):
        self.names = names
        self.tried: list[str] = []

    def get_object(self, service, path, introspect=True):
        assert service == "org.bluez"
        assert introspect is False
        return path


def _interface_factory(bus):
    def factory(obj_path, iface):
        assert iface == "org.freedesktop.DBus.Properties"
        return _FakeProps(bus.names, bus.tried, obj_path)
    return factory


class TestBluezDeviceName:
    MAC = "24:EC:4A:E4:69:A5"

    def test_hinted_adapter_is_tried_first(self):
        bus = _FakeBluezBus({
            "/org/bluez/hci5/dev_24_EC_4A_E4_69_A5": "WD_E7_26ec4ae469a5",
        })
        with patch.object(pp.dbus, "Interface", _interface_factory(bus)):
            name = pp.bluez_device_name(bus, self.MAC, "hci5",
                                        hci_names=["hci0", "hci1", "hci5"])
        assert name == "WD_E7_26ec4ae469a5"
        assert bus.tried == ["/org/bluez/hci5/dev_24_EC_4A_E4_69_A5"]

    def test_falls_through_every_adapter(self):
        bus = _FakeBluezBus({
            "/org/bluez/hci1/dev_24_EC_4A_E4_69_A5": "PMD1234567890123456",
        })
        with patch.object(pp.dbus, "Interface", _interface_factory(bus)):
            name = pp.bluez_device_name(bus, self.MAC, None,
                                        hci_names=["hci0", "hci1"])
        assert name == "PMD1234567890123456"
        assert bus.tried == [
            "/org/bluez/hci0/dev_24_EC_4A_E4_69_A5",
            "/org/bluez/hci1/dev_24_EC_4A_E4_69_A5",
        ]

    def test_no_name_anywhere(self):
        bus = _FakeBluezBus({})
        with patch.object(pp.dbus, "Interface", _interface_factory(bus)):
            assert pp.bluez_device_name(bus, self.MAC, "hci5",
                                        hci_names=["hci0"]) is None

    def test_empty_name_is_no_name(self):
        bus = _FakeBluezBus({"/org/bluez/hci0/dev_24_EC_4A_E4_69_A5": "  "})
        with patch.object(pp.dbus, "Interface", _interface_factory(bus)):
            assert pp.bluez_device_name(bus, self.MAC, None,
                                        hci_names=["hci0"]) is None


# ── Router client ──────────────────────────────────────────────────────────


class _FakeBus:
    def __init__(self, router_owned: bool = False, fail_query: bool = False):
        self.receivers: list[tuple] = []
        self.router_owned = router_owned
        self.fail_query = fail_query

    def add_signal_receiver(self, handler, **kwargs):
        self.receivers.append((handler, kwargs))

    def name_has_owner(self, name):
        if self.fail_query:
            raise RuntimeError("bus gone")
        return self.router_owned and name == pp.ROUTER_BUS_NAME

    # helpers for the tests
    def receivers_for(self, signal_name):
        return [(h, kw) for h, kw in self.receivers
                if kw.get("signal_name") == signal_name]


def _client(bus, known_macs=(), on_adv=None, on_state=None):
    return pp.AdvertisementRouterClient(
        bus,
        on_advertisement=on_adv or MagicMock(),
        on_router_state=on_state or MagicMock(),
        known_macs=known_macs,
    )


class TestAdvertisementRouterClient:
    def test_registrations_exported_on_construction(self):
        # Before start(), before the bus name is claimed: the objects must
        # already be there when the router introspects us.
        bus = _FakeBus()
        client = _client(bus, known_macs=["24:EC:4A:E4:69:A5"])
        assert client.paths == [
            "/ble_advertisements/power_watchdog/mfgr/8978",
            "/ble_advertisements/power_watchdog/addr/24EC4AE469A5",
        ]
        for path, obj in client._registrations.items():
            assert obj.conn is bus
            assert obj.object_path == path

    def test_subscribes_to_advertisement_on_each_registration_path(self):
        bus = _FakeBus()
        client = _client(bus, known_macs=["24:EC:4A:E4:69:A5"])
        subs = bus.receivers_for("Advertisement")
        assert [kw["path"] for _, kw in subs] == client.paths
        assert all(kw["dbus_interface"] == pp.ROUTER_INTERFACE for _, kw in subs)

    def test_start_reports_router_up(self):
        bus = _FakeBus(router_owned=True)
        on_state = MagicMock()
        client = _client(bus, on_state=on_state)
        assert client.available is False
        client.start()
        on_state.assert_called_once_with(True)
        assert client.available is True

    def test_start_reports_router_absent(self):
        bus = _FakeBus(router_owned=False)
        on_state = MagicMock()
        client = _client(bus, on_state=on_state)
        client.start()
        on_state.assert_called_once_with(False)
        assert client.available is False

    def test_start_watches_the_router_name_only(self):
        bus = _FakeBus()
        client = _client(bus)
        client.start()
        subs = bus.receivers_for("NameOwnerChanged")
        assert len(subs) == 1
        _, kw = subs[0]
        assert kw["dbus_interface"] == "org.freedesktop.DBus"
        assert kw["arg0"] == pp.ROUTER_BUS_NAME

    def test_start_is_idempotent(self):
        bus = _FakeBus(router_owned=True)
        on_state = MagicMock()
        client = _client(bus, on_state=on_state)
        client.start()
        client.start()
        assert on_state.call_count == 1
        assert len(bus.receivers_for("NameOwnerChanged")) == 1

    def test_bus_query_failure_reads_as_absent(self):
        bus = _FakeBus(fail_query=True)
        on_state = MagicMock()
        client = _client(bus, on_state=on_state)
        client.start()
        on_state.assert_called_once_with(False)

    def test_name_owner_changes_drive_state(self):
        bus = _FakeBus(router_owned=True)
        on_state = MagicMock()
        client = _client(bus, on_state=on_state)
        client.start()
        handler, _ = bus.receivers_for("NameOwnerChanged")[0]

        handler(pp.ROUTER_BUS_NAME, ":1.50", "")       # router went away
        assert client.available is False
        handler(pp.ROUTER_BUS_NAME, "", ":1.51")       # and came back
        assert client.available is True
        handler(pp.ROUTER_BUS_NAME, "", ":1.51")       # repeat: no new callback
        handler("com.victronenergy.other", ":1.9", "")  # someone else: ignored
        assert [c.args[0] for c in on_state.call_args_list] == [True, False, True]

    def test_signal_arrives_as_plain_python_types(self):
        bus = _FakeBus()
        on_adv = MagicMock()
        client = _client(bus, on_adv=on_adv)
        handler, _ = bus.receivers_for("Advertisement")[0]
        # dbus-python hands over dbus.String / dbus.UInt16 / Array-of-Byte /
        # dbus.Int16; plain equivalents stand in for them here.
        handler("24:ec:4a:e4:69:a5", 8978, [0x45, 0x56, 0, 0], -61, "684E054477B0", "")
        on_adv.assert_called_once_with(
            "24:EC:4A:E4:69:A5", 8978, b"EV\x00\x00", -61, "684E054477B0",
        )

    def test_handler_exception_is_contained(self):
        bus = _FakeBus()
        on_adv = MagicMock(side_effect=RuntimeError("boom"))
        client = _client(bus, on_adv=on_adv)
        handler, _ = bus.receivers_for("Advertisement")[0]
        handler("AA:BB:CC:DD:EE:FF", 8978, [], 0, "hci0", "")  # must not raise

    def test_state_callback_exception_is_contained(self):
        bus = _FakeBus(router_owned=True)
        client = _client(bus, on_state=MagicMock(side_effect=RuntimeError("boom")))
        client.start()  # must not raise
        assert client.available is True

    def test_add_device_registers_once(self):
        bus = _FakeBus()
        client = _client(bus)
        assert client.add_device("24:EC:4A:E4:69:A5") is True
        assert client.add_device("24ec4ae469a5") is False
        assert client.paths[-1] == "/ble_advertisements/power_watchdog/addr/24EC4AE469A5"
        assert len(bus.receivers_for("Advertisement")) == 2
