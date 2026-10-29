"""Planning what plugin devices become, and applying the plan.

Sets are the version fixtures, copied and edited in memory; the machine is
described by the test, so nothing here reads this computer's plugins.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import copy_set, device_named, serum_as
from native_routes import native_route

from abletoolz.formats import live_document
from abletoolz.machine import Locations
from abletoolz.plugins.commands import run_plugin_commands
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.identity.ids import AuIdentifier, PluginKind, Vst2Id
from abletoolz.plugins.identity.plugin_db import PluginDatabase, PluginEntry, PluginSource, write_plugin_db
from abletoolz.plugins.plan import (
    KEEP,
    InstalledVst2,
    Machine,
    Override,
    Plan,
    PluginChoice,
    Route,
    apply_in_place,
    apply_plan,
    dump_plan,
    experiments,
    file_targets,
    load_plan,
    make_plan,
    plan_sets,
    read_plan,
    write_plan,
)
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.fabfilter import PRO_Q1_PARAMETERS
from abletoolz.plugins.state.fxbk import LegacyBank

SERUM_VST3 = (1448301656, 1718835315, 1701999981, 0)
PRO_Q3_VST3 = (1925503857, 2051884442, -1182903948, 1568977821)
SHIMMER = 1936223602
_SHIMMER_MAP = CATALOG.find(Vst2Id(SHIMMER))
assert _SHIMMER_MAP is not None and _SHIMMER_MAP.vst3 is not None and _SHIMMER_MAP.au is not None
SHIMMER_VST3 = _SHIMMER_MAP.vst3.id.uid_fields  # type: ignore[union-attr]
SHIMMER_AU = _SHIMMER_MAP.au.id.identifier  # type: ignore[union-attr]


def machine(
    *,
    vst3: tuple[tuple[int, int, int, int], ...] = (),
    au: tuple[AuIdentifier, ...] = (),
    vst2: tuple[InstalledVst2, ...] = (),
    vst3_names: dict[str, tuple[int, int, int, int]] | None = None,
    broken: bool = True,
) -> Machine:
    names = dict(vst3_names or {})
    names.update({f"installed {index}": fields for index, fields in enumerate(vst3)})
    return Machine(
        vst2=vst2,
        vst3=names,
        au=lambda identifier: identifier in au,
        loadable=lambda info: not broken,
    )


def edit(path: pathlib.Path, change: object) -> None:
    root = live_document.read(path)
    change(root)  # type: ignore[operator]
    path.write_bytes(live_document.encode(root))


def make_pro_q1(root: ET.Element) -> None:
    serum_as(root, "FabFilter Pro-Q", 1179668850, LegacyBank("Air", (0.0,) * PRO_Q1_PARAMETERS).encode())


def make_shimmer(root: ET.Element) -> None:
    serum_as(root, "ValhallaShimmer", SHIMMER)


def choice(plan: Plan, plugin: str) -> PluginChoice:
    """The plan's entry for ``plugin``."""
    return next(item for item in plan.plugins if item.plugin == plugin)


def write_to_disk(target: pathlib.Path, root: ET.Element) -> None:
    target.write_bytes(live_document.encode(root))


def nothing_saved(target: pathlib.Path, root: ET.Element) -> None:
    pytest.fail(f"nothing should be saved, and {target} was")


def test_a_plan_has_one_entry_per_plugin_with_its_devices(tmp_path: pathlib.Path) -> None:
    plan = make_plan([copy_set(tmp_path, name="a.als"), copy_set(tmp_path, name="b.als")], machine(vst3=(SERUM_VST3,)))
    serum = choice(plan, "Serum")
    assert [(ref.set, ref.device) for ref in serum.devices] == [(0, 0), (1, 0)]
    assert serum.format is PluginKind.VST and serum.map_id == "xfer.serum" and serum.broken
    effectrix = choice(plan, "Effectrix")
    assert effectrix.map_id is None and effectrix.targets == [] and effectrix.pick == KEEP


def test_a_plan_names_the_main_track_a_device_sits_on(tmp_path: pathlib.Path) -> None:
    """A Live 12 set calls its master track MainTrack, and a plan used to lose the device's place."""
    path = copy_set(tmp_path, key="12.2.6")
    main = live_document.read(path).find(".//MainTrack/Name/EffectiveName")
    assert main is not None
    plan = make_plan([path], machine())
    (first,) = [ref for choice in plan.plugins for ref in choice.devices if ref.device == 0]
    assert first.track == main.get("Value")


def test_a_mapped_plugin_offers_its_other_format_by_its_plan_name_and_picks_nothing(tmp_path: pathlib.Path) -> None:
    plan = make_plan([copy_set(tmp_path)], machine(vst3=(SERUM_VST3,)))
    serum = choice(plan, "Serum")
    (target,) = serum.targets
    assert (target.id, target.route, target.format, target.installed) == (
        "serum_vst3",
        Route.FORMAT,
        PluginKind.VST3,
        True,
    )
    assert serum.pick == KEEP


def test_a_target_not_installed_here_is_still_offered(tmp_path: pathlib.Path) -> None:
    """Converting for another machine, a Mac friend's AU say, needs no plugin here."""
    plan = make_plan([copy_set(tmp_path)], machine())
    assert [(target.id, target.installed) for target in choice(plan, "Serum").targets] == [("serum_vst3", False)]


def test_another_plugin_is_reached_through_its_converter(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    edit(path, make_pro_q1)
    plan = make_plan([path], machine(vst3=(PRO_Q3_VST3,)))
    pro_q = choice(plan, "FabFilter Pro-Q")
    (target,) = pro_q.targets
    assert (target.id, target.route, target.proved) == ("pro_q_3_vst3", Route.CONVERTER, True)


def test_every_format_the_plugin_has_is_offered(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    edit(path, make_shimmer)
    shimmer = choice(make_plan([path], machine(vst3=(SHIMMER_VST3,), au=(SHIMMER_AU,))), "ValhallaShimmer")
    assert [target.id for target in shimmer.targets] == ["valhalla_shimmer_vst3", "valhalla_shimmer_au"]


def test_a_device_with_saved_control_settings_converts_and_says_what_it_loses(tmp_path: pathlib.Path) -> None:
    """Nothing a converter can't carry refuses a device: the plan shows the cost, and applying reports it."""
    path = copy_set(tmp_path)

    def automate(root: ET.Element) -> None:
        device = device_named(root, "Serum_x64")
        preset = device.info.find("Preset/VstPreset/ParameterSettings")
        assert preset is not None
        ET.SubElement(preset, "PluginParameterSettings")

    edit(path, automate)
    plan = make_plan([path], machine(vst3=(SERUM_VST3,)))
    serum = choice(plan, "Serum")
    (target,) = serum.targets
    assert target.refused == {}
    assert target.warnings == {"its saved per-control settings were dropped": 1}
    assert "1 of 1 device(s): its saved per-control settings were dropped" in dump_plan(plan)

    serum.pick = "serum_vst3"
    (outcome,) = apply_plan(plan, machine=machine(), save=write_to_disk)
    (device,) = outcome.devices
    assert device.done and device.warnings == ["its saved per-control settings were dropped"]


def test_the_plan_file_lists_each_plugin_with_the_names_it_takes_above_it(tmp_path: pathlib.Path) -> None:
    text = dump_plan(make_plan([copy_set(tmp_path)], machine(vst3=(SERUM_VST3,))))
    assert text.startswith("# abletoolz plugin plan")
    serum = "# Serum VST2: 1 device(s), doesn't load here\n#   serum_vst3\n- plugin: Serum\n  format: VST2\n"
    assert serum + "  convert_to: null\n" in text
    entries = [(entry.plugin, entry.format, entry.convert_to) for entry in load_plan(text).plugins]
    assert ("Serum", "VST2", None) in entries


def write_and_pick(tmp_path: pathlib.Path, plan: Plan, plugin: str, convert_to: str) -> pathlib.Path:
    """The plan written to disk with one plugin's convert_to filled in, the way a person edits it."""
    plan_file = tmp_path / "plan.yaml"
    write_plan(plan, plan_file)
    text = plan_file.read_text(encoding="utf-8")
    entry = f"- plugin: {plugin}\n  format: VST2\n  convert_to: null"
    assert entry in text
    plan_file.write_text(text.replace(entry, entry.replace("null", convert_to)), encoding="utf-8")
    return plan_file


def test_convert_to_takes_a_listed_name_and_null_keeps(tmp_path: pathlib.Path) -> None:
    here = machine(vst3=(SERUM_VST3,))
    plan_file = write_and_pick(tmp_path, make_plan([copy_set(tmp_path)], here), "Serum", "serum_vst3")
    plan = read_plan(plan_file, machine=here)
    assert choice(plan, "Serum").pick == "serum_vst3"
    assert all(item.pick == KEEP for item in plan.plugins if item.plugin != "Serum")


@pytest.mark.parametrize("typed", ["Serum_VST3", "serum vst3", "serum_au"])
def test_convert_to_must_be_one_of_the_names_listed_exactly(tmp_path: pathlib.Path, typed: str) -> None:
    here = machine(vst3=(SERUM_VST3,))
    plan_file = write_and_pick(tmp_path, make_plan([copy_set(tmp_path)], here), "Serum", repr(typed))
    with pytest.raises(ValueError, match=f"no target named '{typed}' \\(it can become serum_vst3\\)"):
        read_plan(plan_file, machine=here)


def test_an_entry_for_a_plugin_the_sets_no_longer_hold_is_refused(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    here = machine(vst3=(SERUM_VST3,))
    plan_file = write_and_pick(tmp_path, make_plan([path], here), "Serum", "serum_vst3")
    edit(path, make_shimmer)
    with pytest.raises(ValueError, match="Serum VST2 is not in these sets any more"):
        read_plan(plan_file, machine=here)


def test_a_plan_file_from_another_version_is_refused() -> None:
    with pytest.raises(ValueError, match="version 1, not 2; make a fresh plan"):
        load_plan("abletoolz_plan: 1\nsets: []\nplugins: []\n")


def test_applying_rewrites_the_picked_devices_and_saves_the_set(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    plan = make_plan([path], machine(vst3=(SERUM_VST3,)))
    choice(plan, "Serum").pick = "serum_vst3"
    saved: list[pathlib.Path] = []

    def save(target: pathlib.Path, root: ET.Element) -> None:
        saved.append(target)
        target.write_bytes(live_document.encode(root))

    (outcome,) = apply_plan(plan, machine=machine(), save=save)

    assert outcome.skipped is None and outcome.changed == 1
    assert saved == [path.resolve()]
    kinds = {device.identity.name: device.kind for device in PluginDevices(live_document.read(path))}
    assert kinds["Serum"] is PluginKind.VST3


def test_an_override_keeps_one_device(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    plan = make_plan([path], machine(vst3=(SERUM_VST3,)))
    serum = choice(plan, "Serum")
    serum.pick = "serum_vst3"
    serum.overrides.append(Override(set=0, device=0, pick=KEEP))
    (outcome,) = apply_plan(plan, machine=machine(), save=nothing_saved)
    assert outcome.changed == 0


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (Override(set=0, device=7, pick=KEEP), "does not list"),
        (Override(set=3, device=0, pick=KEEP), "set 3 is not one of the plan's 1 sets"),
    ],
    ids=["a device the entry does not list", "a set the plan does not have"],
)
def test_a_hand_edited_override_naming_nothing_in_the_plan_is_refused(
    tmp_path: pathlib.Path, override: Override, message: str
) -> None:
    """Silently ignoring it would apply the pick the user meant to change; an unknown set used to raise IndexError."""
    plan = make_plan([copy_set(tmp_path)], machine(vst3=(SERUM_VST3,)))
    choice(plan, "Serum").overrides.append(override)
    with pytest.raises(ValueError, match=f"Serum: .*{message}"):
        Plan.model_validate(plan.model_dump())


def test_a_set_that_cannot_be_saved_is_reported_and_the_rest_still_applied(tmp_path: pathlib.Path) -> None:
    first, second = copy_set(tmp_path, name="a.als"), copy_set(tmp_path, name="b.als")
    plan = make_plan([first, second], machine(vst3=(SERUM_VST3,)))
    choice(plan, "Serum").pick = "serum_vst3"

    def save(target: pathlib.Path, root: ET.Element) -> None:
        if target.name == "a.als":
            raise PermissionError("read-only")
        write_to_disk(target, root)

    unsaved, saved = apply_plan(plan, machine=machine(), save=save)
    assert unsaved.skipped == "could not save: read-only" and unsaved.devices == []
    assert saved.skipped is None and saved.changed == 1


def test_a_set_that_changed_since_planning_is_skipped(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    plan = make_plan([path], machine(vst3=(SERUM_VST3,)))
    choice(plan, "Serum").pick = "serum_vst3"
    edit(path, make_shimmer)
    (outcome,) = apply_plan(plan, machine=machine(), save=nothing_saved)
    assert outcome.skipped is not None and "changed since it was planned" in outcome.skipped


def test_a_pick_naming_no_target_is_refused_before_anything_is_written(tmp_path: pathlib.Path) -> None:
    plan = make_plan([copy_set(tmp_path)], machine(vst3=(SERUM_VST3,)))
    choice(plan, "Serum").pick = "serum_au"
    with pytest.raises(ValueError, match="no target named 'serum_au'"):
        apply_plan(plan, machine=machine(), save=nothing_saved)


# -- the same plugin in another file, guesses, and recovery routes -------------------------

SERUM_ID = 1483109208


def test_a_plan_file_never_offers_another_file_or_a_guess(tmp_path: pathlib.Path) -> None:
    """Both are Python only: a plan file offers what a plugin map names by hand."""
    native = InstalledVst2("Serum_x64", SERUM_ID, tmp_path / "VST" / "Serum_x64.vst")
    lookalike = InstalledVst2("Effectrix", 42, tmp_path / "Effectrix.vst")
    plan = make_plan([copy_set(tmp_path)], machine(vst2=(native, lookalike), vst3_names={"Effectrix": (1, 2, 3, 4)}))
    assert [target.route for target in choice(plan, "Serum").targets] == [Route.FORMAT]
    assert choice(plan, "Effectrix").targets == []


def test_applying_a_file_target_points_the_device_at_it_and_keeps_its_id(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)
    native = InstalledVst2("Serum_x64", SERUM_ID, tmp_path / "Plug-Ins" / "Serum_x64.vst")
    here = machine(vst2=(native,))
    plan = make_plan([path], here)
    serum = choice(plan, "Serum")
    serum.targets += file_targets(SERUM_ID, here)
    serum.pick = serum.targets[-1].id
    (outcome,) = apply_plan(plan, machine=machine(), save=write_to_disk)
    assert outcome.changed == 1
    device = next(item for item in PluginDevices(live_document.read(path)) if item.identity.unique_id == SERUM_ID)
    assert device.reference.file_name == "Serum_x64.vst"


def test_a_pick_is_refused_on_a_device_that_holds_another_plugin(tmp_path: pathlib.Path) -> None:
    """Removing a set from the YAML renumbers the rest: Serum's pick must not land on b.als's Shimmer."""
    serum_set, shimmer_set = copy_set(tmp_path, name="a.als"), copy_set(tmp_path, name="b.als")
    edit(shimmer_set, make_shimmer)
    native = InstalledVst2("Serum_x64", SERUM_ID, tmp_path / "Plug-Ins" / "Serum_x64.vst")
    here = machine(vst2=(native,))
    plan = make_plan([serum_set, shimmer_set], here)
    serum = choice(plan, "Serum")
    serum.targets += file_targets(SERUM_ID, here)
    serum.pick = f"file {native.path}"
    before = shimmer_set.read_bytes()
    del plan.sets[0]

    (outcome,) = apply_plan(plan, machine=machine(), save=write_to_disk)

    assert [(device.done, device.reason) for device in outcome.devices] == [
        (False, "the plan lists Serum here, but this device holds another plugin; make a fresh plan")
    ]
    assert shimmer_set.read_bytes() == before


@pytest.mark.parametrize("pick", ["try Effectrix (VST3)", "try file"])
def test_a_picked_guess_writes_the_installed_plugins_identity(tmp_path: pathlib.Path, pick: str) -> None:
    path = copy_set(tmp_path)
    lookalike = InstalledVst2("Effectrix", 42, tmp_path / "Effectrix.vst")
    here = machine(vst2=(lookalike,), vst3_names={"Effectrix": (1, 2, 3, 4)})
    plan = make_plan([path], here)
    effectrix = choice(plan, "Effectrix")
    effectrix.targets += experiments(device_named(live_document.read(path), "Effectrix").identity, here)
    effectrix.pick = next(target.id for target in effectrix.targets if target.id.startswith(pick))

    (outcome,) = apply_plan(plan, machine=machine(), save=write_to_disk)

    assert outcome.changed == 1
    device = device_named(live_document.read(path), "Effectrix")
    if pick.endswith("(VST3)"):
        assert (device.kind, device.identity.uid_fields) == (PluginKind.VST3, (1, 2, 3, 4))
    else:
        assert (device.identity.unique_id, device.saved_state.unique_id) == (42, 42)


def test_a_verified_recovery_route_is_a_target_and_applying_it_relinks_the_native_file(
    tmp_path: pathlib.Path,
) -> None:
    source, _route, route_target = native_route(tmp_path)
    plan = make_plan([source], dataclasses.replace(machine(), recovery=(route_target,)))
    legacy = choice(plan, "Legacy.64")
    (target,) = legacy.targets
    assert (target.id, target.route, target.installed, target.refused) == ("test-native", Route.RECOVERY, True, {})
    legacy.pick = "test-native"

    (outcome,) = apply_plan(
        plan,
        save=write_to_disk,
        machine=dataclasses.replace(machine(), recovery=(route_target,)),
    )

    assert outcome.changed == 1, outcome
    (device,) = PluginDevices(live_document.read(source))
    assert device.reference.file_name == "Native.dll"


# -- the command line ------------------------------------------------------------------


def plugins_command(locations: Locations, *argv: str) -> int:
    """``abletoolz plugins argv`` on the machine at ``locations``, with an empty config."""
    return run_plugin_commands(argv, config=AbletoolzConfig(), locations=locations)


def test_plan_writes_a_file_and_apply_rewrites_the_set_keeping_a_backup(
    tmp_path: pathlib.Path, locations: Locations
) -> None:
    serum = PluginEntry(name="Serum", kind=PluginKind.VST3, source=PluginSource.MODULEINFO, uid_fields=SERUM_VST3)
    write_plugin_db(PluginDatabase(built=datetime.datetime.now(datetime.UTC), plugins=(serum,)), locations.plugin_db)
    sets = tmp_path / "sets"
    sets.mkdir()
    path = copy_set(sets)
    original = path.read_bytes()
    plan_file = tmp_path / "plan.yaml"

    assert plugins_command(locations, "plan", str(sets), "-o", str(plan_file)) == 0
    assert path.read_bytes() == original
    assert plugins_command(locations, "plan", str(sets), "-o", str(plan_file)) == 1  # exists: --force to overwrite
    assert plugins_command(locations, "plan", str(sets), "-o", str(plan_file), "--force") == 0
    assert plugins_command(locations, "apply", str(plan_file)) == 0  # every convert_to is null: nothing happens
    assert path.read_bytes() == original

    entry = "- plugin: Serum\n  format: VST2\n  convert_to: null"
    text = plan_file.read_text(encoding="utf-8")
    assert "#   serum_vst3" in text and entry in text
    plan_file.write_text(text.replace(entry, entry.replace("null", "serum_vst3")), encoding="utf-8")
    assert plugins_command(locations, "apply", str(plan_file)) == 0
    (backup,) = (sets / "abletoolz_backup").iterdir()
    assert backup.read_bytes() == original
    kinds = {device.identity.name: device.kind for device in PluginDevices(live_document.read(path))}
    assert kinds["Serum"] is PluginKind.VST3
    assert plugins_command(locations, "apply", str(plan_file)) == 1  # its Serum VST2 is gone now


def test_a_set_that_is_not_one_fails_the_plan_without_writing_it(tmp_path: pathlib.Path, locations: Locations) -> None:
    broken = tmp_path / "broken.als"
    broken.write_bytes(b"\x1f\x8b\x08\0")
    plan_file = tmp_path / "plan.yaml"
    assert plugins_command(locations, "plan", str(broken), "-o", str(plan_file)) == 1
    assert not plan_file.exists()


def test_a_recovery_route_found_under_plugin_dir_is_planned_and_applied_from_the_command_line(
    tmp_path: pathlib.Path, locations: Locations, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, route, route_target = native_route(tmp_path)
    assert route_target.path is not None
    monkeypatch.setattr("abletoolz.plugins.plan.here.load_recovery_routes", lambda: (route,))
    plan_file = tmp_path / "plan.yaml"
    folder = ["--plugin-dir", str(route_target.path.parent)]
    assert plugins_command(locations, "plan", str(source), "-o", str(plan_file), *folder) == 0
    entry = "- plugin: Legacy.64\n  format: VST2\n  convert_to: null"
    text = plan_file.read_text(encoding="utf-8")
    assert "#   test-native" in text and entry in text
    plan_file.write_text(text.replace(entry, entry.replace("null", "test-native")), encoding="utf-8")
    assert plugins_command(locations, "apply", str(plan_file), *folder) == 0
    (device,) = PluginDevices(live_document.read(source))
    assert device.identity.name == "Native"


def test_a_plan_in_python_is_the_plan_file_planned_edited_and_applied_like_the_command_line(
    tmp_path: pathlib.Path,
) -> None:
    sets = tmp_path / "sets"
    sets.mkdir()
    path = copy_set(sets)
    original = path.read_bytes()
    here = machine(vst3=(SERUM_VST3,))
    plan_file = tmp_path / "plan.yaml"

    write_plan(plan_sets([sets], machine=here), plan_file)
    assert path.read_bytes() == original
    plan = read_plan(plan_file, machine=here)
    choice(plan, "Serum").pick = "serum_vst3"
    (outcome,) = apply_in_place(plan, machine=here)

    assert outcome.changed == 1
    (backup,) = (sets / "abletoolz_backup").iterdir()
    assert backup.read_bytes() == original
    kinds = {device.identity.name: device.kind for device in PluginDevices(live_document.read(path))}
    assert kinds["Serum"] is PluginKind.VST3


def test_planning_a_folder_without_sets_refuses(tmp_path: pathlib.Path) -> None:
    (tmp_path / "abletoolz_backup").mkdir()
    copy_set(tmp_path / "abletoolz_backup")
    with pytest.raises(ValueError, match="No sets found"):
        plan_sets([tmp_path], machine=machine())


# -- what applying does, and what it refuses ---------------------------------------------


def test_applying_a_converter_writes_the_other_plugins_patch(tmp_path: pathlib.Path) -> None:
    from abletoolz.plugins.state.fabfilter import FABFILTER_PROCESSOR_TRAILER, FfbsState

    path = copy_set(tmp_path)
    edit(path, make_pro_q1)
    plan = make_plan([path], machine(vst3=(PRO_Q3_VST3,)))
    choice(plan, "FabFilter Pro-Q").pick = "pro_q_3_vst3"
    (outcome,) = apply_plan(plan, machine=machine(), save=write_to_disk)
    assert outcome.changed == 1
    device = device_named(live_document.read(path), "Pro-Q 3")
    assert device.kind is PluginKind.VST3
    assert FfbsState.parse(device.saved_state.state).tail == FABFILTER_PROCESSOR_TRAILER


def test_a_set_older_than_vst3_refuses_every_vst3_target(tmp_path: pathlib.Path) -> None:
    """A 9.0 set's schema never declared Vst3PluginInfo; Live would reject the whole file."""
    path = copy_set(tmp_path, key="9.0.1")
    plan = make_plan([path], machine(vst3=(PRO_Q3_VST3,)))
    pro_q = choice(plan, "FabFilter Pro-Q")
    (target,) = pro_q.targets
    (reason,) = target.refused
    assert "too old to hold a VST3 device" in reason
    pro_q.pick = "pro_q_3_vst3"
    before = path.read_bytes()
    (outcome,) = apply_plan(plan, machine=machine(), save=nothing_saved)
    assert outcome.changed == 0 and all("too old" in (device.reason or "") for device in outcome.devices)
    assert path.read_bytes() == before


def test_a_stub_device_is_refused_because_the_set_never_says_what_it_is(tmp_path: pathlib.Path) -> None:
    path = copy_set(tmp_path)

    def stub(root: ET.Element) -> None:
        info = device_named(root, "Serum_x64").info
        for tag in ("Category", "Preset"):
            element = info.find(tag)
            assert element is not None
            info.remove(element)

    edit(path, stub)
    plan = make_plan([path], machine(vst3=(SERUM_VST3,)))
    (target,) = choice(plan, "Serum").targets
    assert target.refused == {"the set never says what this device is": 1}


def test_this_machine_says_when_two_sources_disagree_about_a_class_id(caplog: pytest.LogCaptureFixture) -> None:
    """A wrong class id loads another plugin without a word, so the plan's view of this machine shouts."""
    from abletoolz.plugins.identity import plugin_db

    live_says, probe_says = (1, 2, 3, 4), (9, 9, 9, 9)
    database = plugin_db.PluginDatabase(
        built=datetime.datetime.now(),
        plugins=(
            plugin_db.PluginEntry(
                name="Serum", kind=PluginKind.VST3, source=plugin_db.PluginSource.UID_DB, uid_fields=probe_says
            ),
            plugin_db.PluginEntry(
                name="Serum", kind=PluginKind.VST3, source=plugin_db.PluginSource.LIVE_DATABASE, uid_fields=live_says
            ),
        ),
    )
    seen = Machine.of(database, (), loadable=lambda info: True, au=lambda identifier: False)

    assert seen.vst3 == {"Serum": live_says}
    assert "Class id sources disagree about Serum" in caplog.text
