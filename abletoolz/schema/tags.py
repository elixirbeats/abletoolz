"""The names in Live's XML, so code and users stop spelling them from memory.

``Tag`` is the structure of a document -- tracks, chains, clips, samples, the
plugin description -- and ``Attr`` the attributes Live puts its values in.
Native devices and each device's own settings are measured rather than
listed by hand, and live in :mod:`abletoolz.schema.devices`.

A member's value is the tag exactly as Live writes it, so a member goes
anywhere a string does: ``element.find(Tag.DEVICE_CHAIN)``. :func:`path`
joins members into a path both ``Element.find`` and ``elements.get_element``
read. A tag Live renamed across versions has a member per spelling; a
setting's renames are resolved by :mod:`abletoolz.schema.settings`.
"""

from __future__ import annotations

import enum


class Tag(enum.StrEnum):
    """Element names in a Live document, by what they hold."""

    # The document.
    LIVE_SET = "LiveSet"
    TRANSPORT = "Transport"
    NEXT_POINTEE_ID = "NextPointeeId"
    POINTEE = "Pointee"
    POINTEE_ID = "PointeeId"
    KEY_MIDI = "KeyMidi"

    # Tracks. MainTrack is Live 12's name for MasterTrack.
    TRACKS = "Tracks"
    AUDIO_TRACK = "AudioTrack"
    MIDI_TRACK = "MidiTrack"
    GROUP_TRACK = "GroupTrack"
    RETURN_TRACK = "ReturnTrack"
    MAIN_TRACK = "MainTrack"
    MASTER_TRACK = "MasterTrack"
    PRE_HEAR_TRACK = "PreHearTrack"
    NAME = "Name"

    # A track's chain, and the devices in it. A rack holds further chains.
    DEVICE_CHAIN = "DeviceChain"
    DEVICES = "Devices"
    MIXER = "Mixer"
    MAIN_SEQUENCER = "MainSequencer"
    ON = "On"
    MANUAL = "Manual"
    IS_FOLDED = "IsFolded"

    # Mixer and routing. Live 8 sets hold the master's mixer in MasterChain.
    MASTER_CHAIN = "MasterChain"
    TEMPO = "Tempo"
    AUDIO_OUTPUT_ROUTING = "AudioOutputRouting"
    TARGET = "Target"
    LOWER_DISPLAY_STRING = "LowerDisplayString"

    # Arrangement automation.
    ARRANGER_AUTOMATION = "ArrangerAutomation"
    AUTOMATION_ENVELOPES = "AutomationEnvelopes"
    ENVELOPES = "Envelopes"
    AUTOMATION_ENVELOPE = "AutomationEnvelope"
    ENVELOPE_TARGET = "EnvelopeTarget"
    AUTOMATION = "Automation"
    EVENTS = "Events"
    FLOAT_EVENT = "FloatEvent"
    BOOL_EVENT = "BoolEvent"
    ENUM_EVENT = "EnumEvent"
    AUTOMATION_TARGET = "AutomationTarget"
    MODULATION_TARGET = "ModulationTarget"

    # Clips.
    CLIP_SLOT_LIST = "ClipSlotList"
    CLIP_SLOT = "ClipSlot"
    VALUE = "Value"
    AUDIO_CLIP = "AudioClip"
    MIDI_CLIP = "MidiClip"
    CURRENT_END = "CurrentEnd"
    WARP_MARKERS = "WarpMarkers"
    WARP_MARKER = "WarpMarker"
    CLIP_TIMEABLE = "ClipTimeable"
    SAMPLE = "Sample"

    # MIDI notes inside a MidiClip.
    NOTES = "Notes"
    KEY_TRACKS = "KeyTracks"
    KEY_TRACK = "KeyTrack"
    MIDI_KEY = "MidiKey"
    MIDI_NOTE_EVENT = "MidiNoteEvent"
    NOTE_ID_GENERATOR = "NoteIdGenerator"
    NEXT_ID = "NextId"
    PER_NOTE_EVENT_STORE = "PerNoteEventStore"
    EVENT_LISTS = "EventLists"

    # Samples.
    SAMPLE_REF = "SampleRef"
    FILE_REF = "FileRef"
    PATH = "Path"
    RELATIVE_PATH = "RelativePath"
    RELATIVE_PATH_TYPE = "RelativePathType"
    HAS_RELATIVE_PATH = "HasRelativePath"
    RELATIVE_PATH_ELEMENT = "RelativePathElement"
    DATA = "Data"
    ORIGINAL_FILE_SIZE = "OriginalFileSize"
    FILE_SIZE = "FileSize"
    ORIGINAL_CRC = "OriginalCrc"
    CRC = "Crc"
    LAST_MOD_DATE = "LastModDate"
    LIVE_PACK_NAME = "LivePackName"
    SOURCE_CONTEXT = "SourceContext"
    ORIGINAL_FILE_REF = "OriginalFileRef"

    # Hosted plugins.
    PLUGIN_DEVICE = "PluginDevice"
    AU_PLUGIN_DEVICE = "AuPluginDevice"
    PARAMETER_LIST = "ParameterList"
    PARAMETER_ID = "ParameterId"
    PARAMETER_NAME = "ParameterName"
    VISUAL_INDEX = "VisualIndex"
    PARAMETER_VALUE = "ParameterValue"
    MIDI_CONTROLLER_RANGE = "MidiControllerRange"
    LAST_USER_RANGE = "LastUserRange"
    RANGE_MIN = "Min"
    RANGE_MAX = "Max"
    RANGE_FIRST = "First"
    RANGE_LAST = "Last"
    PLUGIN_DESC = "PluginDesc"
    VST_PLUGIN_INFO = "VstPluginInfo"
    VST3_PLUGIN_INFO = "Vst3PluginInfo"
    AU_PLUGIN_INFO = "AuPluginInfo"
    PLUG_NAME = "PlugName"
    DIR = "Dir"
    FILE_NAME = "FileName"
    UNIQUE_ID = "UniqueId"
    CATEGORY = "Category"
    NUMBER_OF_PARAMETERS = "NumberOfParameters"
    NUMBER_OF_PROGRAMS = "NumberOfPrograms"
    FLAGS = "Flags"
    DEVICE_TYPE = "DeviceType"
    UID = "Uid"
    WIN_POS_X = "WinPosX"
    WIN_POS_Y = "WinPosY"
    NUM_AUDIO_INPUTS = "NumAudioInputs"
    NUM_AUDIO_OUTPUTS = "NumAudioOutputs"
    IS_PLACEHOLDER_DEVICE = "IsPlaceholderDevice"
    MANUFACTURER = "Manufacturer"
    COMPONENT_TYPE = "ComponentType"
    COMPONENT_SUB_TYPE = "ComponentSubType"
    COMPONENT_MANUFACTURER = "ComponentManufacturer"
    COMPONENT_FLAGS = "ComponentFlags"
    IS_UNUSABLE = "IsUnusable"
    # An AuPreset's own record of its component, beside Manufacturer and Type.
    SUB_TYPE = "SubType"
    PRESET = "Preset"
    VST_PRESET = "VstPreset"
    VST3_PRESET = "Vst3Preset"
    AU_PRESET = "AuPreset"
    BUFFER = "Buffer"
    PROCESSOR_STATE = "ProcessorState"
    CONTROLLER_STATE = "ControllerState"
    PRESET_REF = "PresetRef"
    # A VST2 preset's own record of the plugin: its container type and counts.
    TYPE = "Type"
    PROGRAM_COUNT = "ProgramCount"
    PARAMETER_COUNT = "ParameterCount"
    PROGRAM_NUMBER = "ProgramNumber"

    # A plugin preset's own settings, shared by VstPreset and Vst3Preset.
    OVERWRITE_PROTECTION_NUMBER = "OverwriteProtectionNumber"
    MPE_ENABLED = "MpeEnabled"
    MPE_SETTINGS = "MpeSettings"
    PARAMETER_SETTINGS = "ParameterSettings"
    IS_ON = "IsOn"
    POWER_MACRO_CONTROL_INDEX = "PowerMacroControlIndex"
    POWER_MACRO_MAPPING_RANGE = "PowerMacroMappingRange"
    STORED_ALL_PARAMETERS = "StoredAllParameters"
    DEVICE_LOM_ID = "DeviceLomId"
    DEVICE_VIEW_LOM_ID = "DeviceViewLomId"
    IS_ON_LOM_ID = "IsOnLomId"
    PARAMETERS_LIST_WRAPPER_LOM_ID = "ParametersListWrapperLomId"


class Attr(enum.StrEnum):
    """Attribute names. Live keeps almost every value in ``Value``."""

    VALUE = "Value"
    ID = "Id"
    CREATOR = "Creator"
    MINOR_VERSION = "MinorVersion"
    DIR = "Dir"

    # Clip events: warp markers and MIDI notes.
    TIME = "Time"
    SEC_TIME = "SecTime"
    BEAT_TIME = "BeatTime"
    DURATION = "Duration"
    VELOCITY = "Velocity"
    VELOCITY_DEVIATION = "VelocityDeviation"
    OFF_VELOCITY = "OffVelocity"
    PROBABILITY = "Probability"
    IS_ENABLED = "IsEnabled"
    NOTE_ID = "NoteId"


def path(*tags: str) -> str:
    """Tags joined into a relative path: ``path(Tag.DEVICE_CHAIN, Tag.MIXER)``."""
    return "/".join(tags)
