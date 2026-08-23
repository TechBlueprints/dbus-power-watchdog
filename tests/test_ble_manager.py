"""Tests for power_watchdog_ble_manager.py — the connection manager wiring.

The module is stdlib only and imports neither bleak nor the library at
module scope, so these run without either: the install path is exercised
against a stub in ``sys.modules`` rather than really rebinding bleak in the
test process.
"""

from __future__ import annotations

import sys
import types

import pytest

from power_watchdog_ble_manager import (
    install_ble_connection_manager,
    load_ble_settings,
    parse_adapters,
    parse_bool,
    parse_link_caps,
    scan_adapter_for,
    split_adapters,
)


class TestParseBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy(self, raw):
        assert parse_bool(raw, default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
    def test_falsey(self, raw):
        assert parse_bool(raw, default=True) is False

    def test_missing_uses_default(self):
        assert parse_bool(None, default=True) is True
        assert parse_bool(None, default=False) is False

    def test_empty_uses_default(self):
        assert parse_bool("  ", default=True) is True

    def test_garbage_uses_default(self):
        # A typo must not silently flip a safety-relevant flag.
        assert parse_bool("maybe", default=True) is True


class TestParseAdapters:
    def test_empty(self):
        assert parse_adapters("") == []
        assert parse_adapters(None) == []

    def test_strips_and_drops_blanks(self):
        assert parse_adapters(" hci0 , ,hci1 ") == ["hci0", "hci1"]

    def test_entries_are_verbatim(self):
        # The library parses these itself; we must not rewrite them.
        assert parse_adapters("AA:BB:CC:DD:EE:FF@hci1") == [
            "AA:BB:CC:DD:EE:FF@hci1"
        ]


class TestParseLinkCaps:
    def test_valid(self):
        assert parse_link_caps("hci0:5, hci1:7") == {"hci0": 5, "hci1": 7}

    def test_empty(self):
        assert parse_link_caps("") == {}

    def test_skips_malformed(self):
        # A wrong cap silently gates connections, so a bad entry is dropped
        # rather than guessed at.
        assert parse_link_caps("hci0:5, hci1:lots, hci2, hci3:0, :4") == {
            "hci0": 5,
        }


class TestSplitAdapters:
    def test_pool_only(self):
        assert split_adapters(["hci0", "hci1"]) == ({}, ["hci0", "hci1"])

    def test_pins_are_upper_cased(self):
        pins, pool = split_adapters(["aa:bb:cc:dd:ee:ff@hci1"])
        assert pins == {"AA:BB:CC:DD:EE:FF": ["hci1"]}
        assert pool == []

    def test_repeated_mac_builds_preference_order(self):
        pins, _ = split_adapters(["AA:BB@hci1", "AA:BB@hci2", "AA:BB@hci1"])
        assert pins == {"AA:BB": ["hci1", "hci2"]}

    def test_mixed(self):
        pins, pool = split_adapters(["hci0", "AA:BB@hci1"])
        assert pins == {"AA:BB": ["hci1"]}
        assert pool == ["hci0"]

    def test_malformed_pin_is_skipped(self):
        assert split_adapters(["@hci0", "AA:BB@"]) == ({}, [])


class TestScanAdapterFor:
    def test_no_config_means_bleak_default(self):
        assert scan_adapter_for("AA:BB", []) is None

    def test_pool_entry_used_when_unpinned(self):
        assert scan_adapter_for("AA:BB", ["hci1"]) == "hci1"

    def test_pin_wins_over_pool(self):
        # Scanning on the pinned card is what makes the device resolve to a
        # D-Bus path on that card, which is where the link is then made.
        assert scan_adapter_for("AA:BB", ["hci0", "AA:BB@hci2"]) == "hci2"

    def test_pin_matched_case_insensitively(self):
        assert scan_adapter_for("aa:bb", ["AA:BB@hci2"]) == "hci2"

    def test_first_pin_is_used(self):
        assert scan_adapter_for("AA:BB", ["AA:BB@hci2", "AA:BB@hci3"]) == "hci2"

    def test_discovery_scan_ignores_pins(self):
        # A discovery scan is not looking for one MAC, so only the pool
        # applies; a pins-only config leaves it on bleak's default.
        assert scan_adapter_for(None, ["AA:BB@hci2"]) is None
        assert scan_adapter_for(None, ["hci1", "AA:BB@hci2"]) == "hci1"


class TestLoadBleSettings:
    def test_prefers_config_ini(self, tmp_path):
        (tmp_path / "config.ini").write_text(
            "[DEFAULT]\nbluetooth_adapters = hci9\n"
        )
        (tmp_path / "config.default.ini").write_text(
            "[DEFAULT]\nbluetooth_adapters = hci0\n"
        )
        assert load_ble_settings(str(tmp_path))["bluetooth_adapters"] == "hci9"

    def test_falls_back_to_default_ini(self, tmp_path):
        (tmp_path / "config.default.ini").write_text(
            "[DEFAULT]\nble_connection_manager = false\n"
        )
        settings = load_ble_settings(str(tmp_path))
        assert settings["ble_connection_manager"] == "false"

    def test_no_config_at_all(self, tmp_path):
        assert load_ble_settings(str(tmp_path)) == {}


class _StubLibrary:
    """Stands in for the real package so no test rebinds bleak."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple] = []
        self._raises = raises

    def install_bleak_catcher(self, owner, **kwargs):
        self.calls.append((owner, kwargs))
        if self._raises is not None:
            raise self._raises


@pytest.fixture
def stub_library(monkeypatch):
    def _install(raises: Exception | None = None) -> _StubLibrary:
        stub = _StubLibrary(raises)
        module = types.ModuleType("bleak_connection_manager")
        module.install_bleak_catcher = stub.install_bleak_catcher
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", module)
        return stub

    return _install


class TestInstall:
    def test_disabled_by_config(self, stub_library):
        stub = stub_library()
        installed = install_ble_connection_manager(
            settings={"ble_connection_manager": "false"},
        )
        assert installed is False
        assert stub.calls == []

    def test_enabled_by_default(self, stub_library):
        # No key at all means on: an install upgrading from the v1 manager
        # keeps the coordination it already had.
        stub = stub_library()
        assert install_ble_connection_manager(settings={}) is True
        assert len(stub.calls) == 1

    def test_config_is_passed_through(self, stub_library):
        stub = stub_library()
        install_ble_connection_manager(
            owner="test-owner",
            settings={
                "bluetooth_adapters": "hci0, AA:BB@hci1",
                "ble_link_caps": "hci0:5",
                "ble_wrap_scanner": "true",
            },
        )
        owner, kwargs = stub.calls[0]
        assert owner == "test-owner"
        assert kwargs["adapters"] == ["hci0", "AA:BB@hci1"]
        assert kwargs["link_caps"] == {"hci0": 5}
        assert kwargs["wrap_scanner"] is True

    def test_scanner_wrapping_is_off_by_default(self, stub_library):
        stub = stub_library()
        install_ble_connection_manager(settings={})
        assert stub.calls[0][1]["wrap_scanner"] is False

    def test_failure_is_swallowed(self, stub_library):
        # Coordination is an optimization: connecting uncoordinated beats
        # not connecting at all.
        stub_library(raises=RuntimeError("no claim dir"))
        assert install_ble_connection_manager(settings={}) is False

    def test_missing_library_is_swallowed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", None)
        assert install_ble_connection_manager(settings={}) is False


class TestImportSideEffects:
    def test_importing_the_service_does_not_install_the_catcher(self):
        # The install is guarded on __main__: loading the service file (as
        # the test suite does) must not rebind bleak or take claim files in
        # a process that only wanted to read the module.
        import importlib.util
        from pathlib import Path

        import bleak

        service_file = (
            Path(__file__).resolve().parent.parent / "dbus-power-watchdog.py"
        )
        spec = importlib.util.spec_from_file_location(
            "pw_service_import_guard", str(service_file),
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["pw_service_import_guard"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            del sys.modules["pw_service_import_guard"]

        assert bleak.BleakClient.__module__.startswith("bleak")
        assert "bleak_connection_manager" not in bleak.BleakClient.__module__


# ── Shared-stack resolution ───────────────────────────────────────────────
#
# Production runs on /data/bcm via the interpreter shim (service/run), which
# puts the BLE stack on PYTHONPATH. These cover the standalone fallback: it
# must fill in for a bare clone, and must never shadow a stack the
# interpreter already provides.

import importlib.util  # noqa: E402
import os  # noqa: E402

from power_watchdog_ble_manager import _ensure_ble_stack  # noqa: E402


def _ext_paths_in(path_list):
    return [p for p in path_list if os.sep + "ext" + os.sep in p]


class TestEnsureBleStack:
    def test_noop_when_already_imported(self, monkeypatch):
        # The cheap check first: the package is in sys.modules, so nothing
        # is resolved and nothing is inserted.
        monkeypatch.setitem(
            sys.modules, "bleak_connection_manager", types.ModuleType("x"),
        )
        before = list(sys.path)
        monkeypatch.setattr(sys, "path", list(sys.path))
        _ensure_ble_stack()
        assert sys.path == before

    def test_noop_when_the_interpreter_provides_it(self, monkeypatch):
        # What happens under the shim: not yet imported, but importable, so
        # the vendored copies must not shadow /data/bcm's.
        monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name: object(),
        )
        monkeypatch.setattr(sys, "path", ["/only-this"])
        _ensure_ble_stack()
        assert sys.path == ["/only-this"]

    def test_inserts_vendored_paths_when_absent(self, monkeypatch):
        # A bare clone: nothing provides the stack, so ext/ fills in.
        monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(sys, "path", ["/only-this"])
        _ensure_ble_stack()
        inserted = _ext_paths_in(sys.path)
        assert inserted, "expected the vendored ext/ paths to be inserted"
        # bleak itself is what the fallback has to supply; the connection
        # manager is deliberately NOT vendored — it comes from /data/bcm or
        # not at all, and its absence degrades to connecting uncoordinated.
        assert any(p.endswith(os.sep + "bleak") for p in inserted)
        assert not any("bleak-connection-manager" in p for p in inserted)

    def test_find_spec_valueerror_degrades_to_inserting(self, monkeypatch):
        # A stubbed module with __spec__ = None makes find_spec raise
        # ValueError. The safe direction is to insert, never to run with no
        # stack at all.
        def boom(name):
            raise ValueError("__spec__ is None")

        monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)
        monkeypatch.setattr(importlib.util, "find_spec", boom)
        monkeypatch.setattr(sys, "path", ["/only-this"])
        _ensure_ble_stack()
        assert _ext_paths_in(sys.path)

    def test_install_resolves_the_stack_even_when_disabled(self, monkeypatch):
        # power_watchdog_ble imports bleak whether or not the catcher is on,
        # so resolution must happen before the enabled check.
        calls = []
        monkeypatch.setattr(
            "power_watchdog_ble_manager._ensure_ble_stack",
            lambda: calls.append(True),
        )
        install_ble_connection_manager(
            settings={"ble_connection_manager": "false"},
        )
        assert calls == [True]
