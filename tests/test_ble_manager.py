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

    def test_failure_is_swallowed(self, stub_library, caplog):
        # Coordination is an optimization: connecting uncoordinated beats
        # not connecting at all.  Sixth contract line, verbatim: the import
        # worked, so this is the driver's or catcher's fault, not the
        # install's.
        stub_library(raises=RuntimeError("no claim dir"))
        with caplog.at_level("ERROR", logger="power_watchdog_ble_manager"):
            assert install_ble_connection_manager(settings={}) is False
        assert [r.message for r in caplog.records] == [
            "BLE coordination: catcher would not install from /data/bcm, "
            "running uncoordinated: RuntimeError('no claim dir')"
        ]

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


# ── MAC-named adapters (2026-08-27) ───────────────────────────────────────
#
# hciN is not an identity: a replug or reboot renumbers the cards. Adapters
# are named by MAC in config and translated to the current hciN as late as
# possible, because bleak's adapter= kwarg accepts nothing else.

from power_watchdog_ble_manager import resolve_adapter  # noqa: E402


@pytest.fixture
def stub_claims(monkeypatch):
    """Stand in for bleak_connection_manager.claims with a fixed mapping."""

    def _install(mapping, raises=False):
        calls = []

        def hci_for(entry, fresh=True):
            calls.append((entry, fresh))
            if raises:
                raise RuntimeError("hciconfig unavailable")
            return mapping.get(entry)

        claims = types.ModuleType("bleak_connection_manager.claims")
        claims.hci_for = hci_for
        pkg = types.ModuleType("bleak_connection_manager")
        pkg.claims = claims
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", pkg)
        monkeypatch.setitem(sys.modules, "bleak_connection_manager.claims", claims)
        return calls

    return _install


class TestResolveAdapter:
    def test_mac_resolves_to_current_hci(self, stub_claims):
        stub_claims({"00:1A:7D:DA:71:07": "hci2"})
        assert resolve_adapter("00:1A:7D:DA:71:07") == "hci2"

    def test_same_mac_can_resolve_elsewhere_after_renumber(self, stub_claims):
        # The whole point: the card moved, the config did not.
        stub_claims({"00:1A:7D:DA:71:07": "hci5"})
        assert resolve_adapter("00:1A:7D:DA:71:07") == "hci5"

    def test_hci_name_still_accepted(self, stub_claims):
        stub_claims({"hci2": "hci2"})
        assert resolve_adapter("hci2") == "hci2"

    def test_absent_card_yields_none_not_a_bogus_adapter(self, stub_claims):
        # Handing bleak an unresolvable MAC, or an hciN that now belongs to
        # another radio, is worse than letting it choose.
        stub_claims({})
        assert resolve_adapter("00:1A:7D:DA:71:07") is None

    def test_lookup_failure_yields_none(self, stub_claims):
        stub_claims({}, raises=True)
        assert resolve_adapter("00:1A:7D:DA:71:07") is None

    def test_resolution_is_fresh(self, stub_claims):
        # A cached answer is useless for a value whose whole premise is
        # that it changes.
        calls = stub_claims({"00:1A:7D:DA:71:07": "hci2"})
        resolve_adapter("00:1A:7D:DA:71:07")
        assert calls == [("00:1A:7D:DA:71:07", True)]

    def test_no_shared_stack_uses_an_hci_name_verbatim(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", None)
        assert resolve_adapter("hci2") == "hci2"

    def test_no_shared_stack_drops_a_mac(self, monkeypatch):
        # Contract rule 7: on the standalone path hand plain bleak nothing
        # it cannot parse.  A MAC is the library's syntax, not bleak's.
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", None)
        assert resolve_adapter("00:1A:7D:DA:71:07") is None

    def test_empty_entry(self):
        assert resolve_adapter(None) is None
        assert resolve_adapter("") is None


class TestScanAdapterForWithMacs:
    def test_device_pinned_to_a_mac_named_card(self, stub_claims):
        stub_claims({"00:1A:7D:DA:71:07": "hci2"})
        entries = ["24:EC:4A:E4:69:A5@00:1A:7D:DA:71:07"]
        assert scan_adapter_for("24:EC:4A:E4:69:A5", entries) == "hci2"

    def test_mac_pool_entry(self, stub_claims):
        stub_claims({"00:1A:7D:DA:71:07": "hci2"})
        assert scan_adapter_for("AA:BB", ["00:1A:7D:DA:71:07"]) == "hci2"

    def test_pin_still_wins_over_pool(self, stub_claims):
        stub_claims({"00:1A:7D:DA:71:07": "hci2", "68:4E:05:44:77:B0": "hci0"})
        entries = ["68:4E:05:44:77:B0", "AA:BB@00:1A:7D:DA:71:07"]
        assert scan_adapter_for("AA:BB", entries) == "hci2"


# ── Shared-install discovery (2026-09-06) ─────────────────────────────────
#
# The service finds /data/bcm itself (ble_stack.py, lifted verbatim from the
# fleet reference) instead of being launched through an interpreter shim.
# Nothing about how the process was launched may decide which stack it runs
# on, and the connection manager must be importable before bleak whether or
# not the catcher is enabled.

import os  # noqa: E402

import ble_stack  # noqa: E402
import power_watchdog_ble_manager as manager  # noqa: E402


@pytest.fixture
def clean_import_state():
    """Restore sys.path and drop any module a test imported from tmp."""
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    ble_stack.shared_failure = None
    yield
    sys.path[:] = path_before
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]
    sys.modules.pop("bleak_connection_manager", None)
    ble_stack.shared_failure = None


def _fake_shared_install(root, body="MARK = 'shared'\n"):
    pkg = root / "src" / "bleak_connection_manager"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(body)
    return str(root)


class TestEnsureBleStack:
    def test_shared_install_goes_to_the_front_of_sys_path(
        self, tmp_path, clean_import_state,
    ):
        root = _fake_shared_install(tmp_path)
        assert ble_stack.ensure_ble_stack(root, vendored_dir=None) == "shared"
        # src first, then ext, then the two upstream trees -- import
        # priority order, ahead of everything the interpreter had.
        assert sys.path[:4] == ble_stack.shared_lib_paths(root)
        import bleak_connection_manager

        assert bleak_connection_manager.MARK == "shared"
        assert ble_stack.shared_failure is None

    def test_absent_install_inserts_nothing(self, tmp_path, clean_import_state):
        before = list(sys.path)
        result = ble_stack.ensure_ble_stack(
            str(tmp_path / "nope"), vendored_dir=None,
        )
        assert result == "vendored"
        assert sys.path == before
        assert ble_stack.shared_failure is None
        assert "bleak_connection_manager" not in sys.modules

    def test_broken_install_is_withdrawn_entirely(
        self, tmp_path, clean_import_state,
    ):
        # Present but unusable must not leave half a stack on the path: a
        # bleak from /data/bcm with no catcher would be a silent downgrade.
        root = _fake_shared_install(
            tmp_path, body="raise RuntimeError('half-installed')\n",
        )
        before = list(sys.path)
        assert ble_stack.ensure_ble_stack(root, vendored_dir=None) == "vendored"
        assert sys.path == before
        assert "RuntimeError" in ble_stack.shared_failure
        assert "half-installed" in ble_stack.shared_failure
        assert not any(
            (getattr(m, "__file__", None) or "").startswith(root)
            for m in sys.modules.values()
        )

    def test_already_imported_manager_is_left_alone(
        self, tmp_path, clean_import_state, monkeypatch,
    ):
        # A launcher or a test stub that already provided the package wins;
        # the shared dir is not even looked at.
        root = _fake_shared_install(tmp_path)
        monkeypatch.setitem(
            sys.modules, "bleak_connection_manager",
            types.ModuleType("bleak_connection_manager"),
        )
        before = list(sys.path)
        assert ble_stack.ensure_ble_stack(root, vendored_dir=None) == "provided"
        assert sys.path == before

    def test_manager_is_importable_before_bleak(
        self, tmp_path, clean_import_state,
    ):
        # The contract: import the connection manager FIRST, so a box-wide
        # autowire hook stands down for this process.  ensure_ble_stack
        # itself performs that import.
        root = _fake_shared_install(tmp_path)
        ble_stack.ensure_ble_stack(root, vendored_dir=None)
        assert "bleak_connection_manager" in sys.modules


@pytest.fixture
def spy_ensure(monkeypatch):
    """Replace ble_stack.ensure_ble_stack with a recorder.

    Answers "provided" (the test-stub state) unless the test sets
    ``spy_ensure.mode`` first.
    """
    calls = []

    def _ensure(shared_dir, vendored_dir=ble_stack.EXT_BLE):
        calls.append((shared_dir, vendored_dir))
        return _ensure.mode

    _ensure.mode = "provided"
    _ensure.calls = calls
    monkeypatch.setattr(ble_stack, "ensure_ble_stack", _ensure)
    return _ensure


class TestInstallFindsTheStack:
    def test_default_shared_dir(self, spy_ensure, stub_library):
        stub_library()
        install_ble_connection_manager(settings={})
        assert spy_ensure.calls == [(manager.DEFAULT_SHARED_DIR, None)]
        assert manager.DEFAULT_SHARED_DIR == "/data/bcm"

    def test_shared_dir_comes_from_config(self, spy_ensure, stub_library):
        stub_library()
        install_ble_connection_manager(
            settings={"ble_connection_manager_dir": " /data/bcm-canary "},
        )
        assert spy_ensure.calls == [("/data/bcm-canary", None)]

    def test_empty_setting_means_never_look(
        self, spy_ensure, stub_library, caplog,
    ):
        # Contract rule 1: an EMPTY value is a deliberate standalone run,
        # not a request for the default.
        stub = stub_library()
        with caplog.at_level("WARNING", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(
                settings={"ble_connection_manager_dir": ""},
            )
        assert spy_ensure.calls == []
        # ...and the catcher is still installed if something provides it.
        assert len(stub.calls) == 1
        # Coordination requested but nowhere to look: the contract's
        # fourth line, verbatim.
        assert [r.message for r in caplog.records] == [
            "BLE coordination: ble_connection_manager is on but "
            "ble_connection_manager_dir is empty; running uncoordinated, "
            "no claims, no adapter routing, no card recovery"
        ]

    def test_empty_setting_with_manager_off_logs_no_line(
        self, spy_ensure, stub_library, caplog,
    ):
        stub_library()
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={
                "ble_connection_manager_dir": "",
                "ble_connection_manager": "false",
            })
        assert not any("BLE coordination" in r.message for r in caplog.records)

    def test_no_vendored_fallback_is_ever_offered(self, spy_ensure, stub_library):
        # This repo vendors nothing; offering ext/ would be a stale-copy trap.
        stub_library()
        install_ble_connection_manager(settings={})
        assert spy_ensure.calls[0][1] is None

    def test_stack_is_found_even_when_the_catcher_is_disabled(
        self, spy_ensure, stub_library,
    ):
        # Importability is not a feature flag: power_watchdog_ble still
        # needs bleak, and the shared install is the only place it lives.
        stub = stub_library()
        install_ble_connection_manager(
            settings={"ble_connection_manager": "false"},
        )
        assert len(spy_ensure.calls) == 1
        assert stub.calls == []

    def test_start_notify_policy_defaults_on(self, spy_ensure, stub_library):
        # Fleet policy used to arrive via the shim's environment; with a
        # plain launcher it is a consumer-side key, default true.
        stub = stub_library()
        install_ble_connection_manager(settings={})
        assert stub.calls[0][1]["force_start_notify"] is True

    def test_start_notify_policy_can_be_switched_off(
        self, spy_ensure, stub_library,
    ):
        stub = stub_library()
        install_ble_connection_manager(
            settings={"ble_force_start_notify": "false"},
        )
        assert stub.calls[0][1]["force_start_notify"] is False

    def test_legacy_install_gets_the_policy_through_the_environment(
        self, spy_ensure, monkeypatch, caplog,
    ):
        # Fifth contract line: a shared install older than 159536a has no
        # force_start_notify= parameter.  Detect it from the signature,
        # set the legacy variable, and warn -- the monitor treats this as
        # a raise whose operator action is "update the install".
        calls = []

        def legacy_install(owner, adapters=(), link_caps=None,
                           wrap_scanner=False):
            calls.append((owner, adapters, link_caps, wrap_scanner))

        module = types.ModuleType("bleak_connection_manager")
        module.install_bleak_catcher = legacy_install
        monkeypatch.setitem(sys.modules, "bleak_connection_manager", module)
        monkeypatch.delenv("BCM_FORCE_START_NOTIFY", raising=False)
        with caplog.at_level("WARNING", logger="power_watchdog_ble_manager"):
            assert install_ble_connection_manager(
                settings={"ble_force_start_notify": "false"},
            ) is True
        assert len(calls) == 1
        assert os.environ["BCM_FORCE_START_NOTIFY"] == "false"
        assert [r.message for r in caplog.records] == [
            "BLE coordination: shared install at /data/bcm predates the "
            "force_start_notify parameter; StartNotify policy passed through "
            "the legacy BCM_FORCE_START_NOTIFY environment"
        ]

    def test_current_install_does_not_touch_the_environment(
        self, spy_ensure, stub_library, monkeypatch, caplog,
    ):
        # The stub takes **kwargs, which counts as accepting the parameter.
        stub_library()
        monkeypatch.delenv("BCM_FORCE_START_NOTIFY", raising=False)
        with caplog.at_level("WARNING", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={})
        assert "BCM_FORCE_START_NOTIFY" not in os.environ
        assert not any("predates" in r.message for r in caplog.records)

    def test_loaded_from_is_logged_for_a_shared_install(
        self, spy_ensure, stub_library, caplog,
    ):
        stub_library()
        sys.modules["bleak_connection_manager"].__file__ = (
            "/data/bcm/src/bleak_connection_manager/__init__.py"
        )
        spy_ensure.mode = "shared"
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={})
        infos = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert (
            "BLE coordination: bleak_connection_manager loaded from "
            "/data/bcm/src/bleak_connection_manager"
        ) in infos

    def test_catcher_installed_line(self, spy_ensure, stub_library, caplog):
        # Seventh contract line, verbatim: the library's own install INFO
        # never reaches a consumer log, so the consumer says it.
        stub_library()
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={
                "bluetooth_adapters": (
                    "00:01:95:C9:B2:EA, "
                    "24:EC:4A:E4:69:A5@00:01:95:C9:B2:EA, "
                    "24:EC:4A:E4:69:A5@00:01:95:CC:33:0B"
                ),
            })
        infos = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert infos[0] == (
            "BLE coordination: catcher installed (force_start_notify=True, "
            "adapters=3 configured, 2 pinned)"
        )

    def test_catcher_installed_line_with_policy_off_and_no_adapters(
        self, spy_ensure, stub_library, caplog,
    ):
        stub_library()
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(
                settings={"ble_force_start_notify": "no"},
            )
        infos = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert infos[0] == (
            "BLE coordination: catcher installed (force_start_notify=False, "
            "adapters=0 configured, 0 pinned)"
        )

    def test_installed_line_follows_loaded_from(
        self, spy_ensure, stub_library, caplog,
    ):
        stub_library()
        sys.modules["bleak_connection_manager"].__file__ = (
            "/data/bcm/src/bleak_connection_manager/__init__.py"
        )
        spy_ensure.mode = "shared"
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={})
        contract = [
            r.message for r in caplog.records
            if r.message.startswith("BLE coordination: ")
        ]
        assert contract[0].startswith("BLE coordination: bleak_connection_manager loaded from ")
        assert contract[1].startswith("BLE coordination: catcher installed (")
        assert len(contract) == 2

    def test_provided_install_logs_nothing_about_loading(
        self, spy_ensure, stub_library, caplog,
    ):
        # "provided" inserts nothing, so there is nothing to report about
        # loading; the "catcher installed" line still follows.
        stub_library()
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={})
        assert not any("loaded from" in r.message for r in caplog.records)


class TestInstallWithoutTheStack:
    def test_absent_install_is_a_warning(
        self, tmp_path, clean_import_state, caplog,
    ):
        # The normal state of any box without the shared install.
        missing = str(tmp_path / "nope")
        with caplog.at_level("WARNING", logger="power_watchdog_ble_manager"):
            assert install_ble_connection_manager(
                settings={"ble_connection_manager_dir": missing},
            ) is False
        contract = [
            r for r in caplog.records if r.message.startswith("BLE coordination")
        ]
        assert [r.levelname for r in contract] == ["WARNING"]
        # Verbatim: the fleet monitor greps for this string.
        assert contract[0].message == (
            "BLE coordination: no shared install at %s; running "
            "uncoordinated, no claims, no adapter routing, no card recovery"
            % missing
        )

    def test_manager_deliberately_off_logs_no_line(
        self, tmp_path, clean_import_state, spy_ensure, caplog,
    ):
        # The stack is still made importable, silently: the coordination
        # lines are for a manager that is on.
        missing = str(tmp_path / "nope")
        spy_ensure.mode = "vendored"
        with caplog.at_level("INFO", logger="power_watchdog_ble_manager"):
            install_ble_connection_manager(settings={
                "ble_connection_manager_dir": missing,
                "ble_connection_manager": "false",
            })
        assert spy_ensure.calls == [(missing, None)]
        assert not any("BLE coordination" in r.message for r in caplog.records)

    def test_broken_install_is_an_error(
        self, tmp_path, clean_import_state, caplog,
    ):
        # Present but unusable is an operator action, not background noise.
        # A real half-installed tree, so the loader itself records why.
        root = _fake_shared_install(
            tmp_path, body="raise RuntimeError('half-installed')\n",
        )
        with caplog.at_level("WARNING", logger="power_watchdog_ble_manager"):
            assert install_ble_connection_manager(
                settings={"ble_connection_manager_dir": root},
            ) is False
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        # Verbatim: "... at <DIR> is present but unusable, running
        # uncoordinated: <repr(exc)>".
        assert errors[0].message == (
            "BLE coordination: shared install at %s is present but unusable, "
            "running uncoordinated: RuntimeError('half-installed')" % root
        )
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
