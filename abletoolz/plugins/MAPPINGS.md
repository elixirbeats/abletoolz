# Plugin maps and conversions

Here's what abletoolz can read, edit and convert so far. Some plugins are only partly mapped; the notes say what works and what is still missing. The rest of the [research progress](../../doc/PLUGIN_MAPPING_RESEARCH.md) is listed separately.

## Mapped plugins

These maps let you work with saved plugin settings without opening Live. The [Python guide](../../doc/PLUGIN_STATE_MODELS.md) explains how to use them.

| Plugin | What is mapped | Notes |
| --- | --- | --- |
| [Trash 1](state/izotope_trash.py) | All 16 presets in a bank; 296 settings and 182 sampled controls for revision 21; 48 distortion algorithms | Revision 22 banks can be read and saved, but their settings cannot be edited yet. |
| [Trash 2](state/izotope_trash2.py) | 64 saved settings, 16 control curves and 52 distortion algorithms; single-band polarity | Tested with one VST2 build and its saved-state layout. |
| [FabFilter Pro-Q 1, 2 and 3](state/fabfilter.py) | EQ bands, global settings, preset names and the differences between versions | The converters below cover the supported settings. |
| [FabFilter Pro-C 2](state/fabfilter.py) | All 46 controls and how their values are saved | Includes the [measured control table](state/data/pro-c-2.json). |
| [FabFilter Saturn 2, Timeless 3 and Volcano 3](state/fabfilter.py) | Saved processor and editor data | The full control maps are still missing. |
| [Older FabFilter plugins](state/fxbk.py) | Preset names and the banks of values saved by the host | Reading the values alone does not tell us what every control means. |
| [Older Analog Lab](state/arturia.py) | Preset names, version and instrument identity in the observed format | Synth settings inside the preset are kept as they are. |
| [Serato Sample](parsers/serato.py) | Sample paths and the information needed to relink them | Saved as JSON. |
| [Maschine 2](parsers/maschine.py) | Sample and kit references | Read-only for now. |
| [xfadelooper / xfadelooper.64](parsers/xfadelooper.py) | Preset name, sample path and 88 saved values | Sample paths can be edited; control meanings still need mapping. |

## Conversions

See [plugin commands and configuration](../../README.md#plugins) for the current setup. Trash conversion is currently available from Python; applying it to a set still needs wiring up. The links below point to the implementations.

| From | To | What works | Limits |
| --- | --- | --- | --- |
| [Trash 1](converters/izotope_trash.py) | Trash 2 | Two single-band distortion stages, with 47 algorithm matches and gain, drive, style, mix and polarity settings | Choose a preset explicitly. Other modules and multiband modes must be off. Algorithm 36 has no match. Five old presets were checked against native saves and audio; some algorithms sound different at other settings and return a notice. |
| [FabFilter Pro-Q 1](state/fabfilter.py) | Pro-Q 3 | EQ settings and preset name | Stereo bands only; other band placements are refused. Automation needs its own translation. |
| [FabFilter Pro-Q 2](state/fabfilter.py) | Pro-Q 3 | Supported EQ settings | Checked in the target plugin. Unsupported monitoring modes are refused. |
| [FabFilter Pro-C 2 VST2](state/fabfilter.py) | Pro-C 2 VST3 | All 46 controls | Nine real presets were loaded and saved back exactly. |
| [Pro-Q 3, Saturn 2, Timeless 3 and Volcano 3 VST2](state/fabfilter.py) | The same plugin in VST3 | Processor state, with product-specific editor/controller handling | Target identity and controller settings must be configured for the product. |
| [kHs Distortion / Filter VST2](container/format_translation.py) | The same plugin in VST3 | Saved settings with the new wrapper | Loaded in Live and checked by ear. |
| [Serum, Ghz Tupe 3, Serato Sample and Prophet V3 VST2](container/format_translation.py) | The same plugin in VST3 | The saved state carries across unchanged | Loaded in Live and checked by ear. |
| [iZotope's measured VST2 wrapper](state/izotope.py) | VST3 processor state | Removes the length and preset-name wrapper | This only handles the wrapper; it does not upgrade the plugin version. |
| [Massive 1.7 VST2](state/measured.py) | Massive 1.7 VST3 | The same saved state loads in both versions | Checked in the native hosts; other NI plugins and versions need their own checks. |

The [state model guide](../../doc/PLUGIN_STATE_MODELS.md) has the Python interfaces. [Measured compatibility](state/measured.py) records what was checked for each plugin. Loading the right values and sounding the same are checked separately.

## Recovering wrapped plugins

These are the old wrapper references we can replace with native 64-bit versions. Each route only accepts the saved states, presets and plugin builds we've checked, listed in the [route data](data/native_recovery.json). It does not convert to a different product. Original 32-bit audio has not been compared for these routes.

| Plugin | Saved states checked | What was checked | Notes |
| --- | ---: | --- | --- |
| Audio Damage DubStation 1.5 | 23 | Entire bank and selected preset | Exact saved bytes |
| Sonimus Satson Channel | 7 | Entire bank and selected preset | Exact saved bytes |
| Sonimus Satson Buss | 1 | Entire bank and selected preset | Exact saved bytes |
| Audio Damage Ratshack Reverb 2 | 6 | Entire bank and selected preset | Exact saved bytes |
| Sonic Charge Permut8 | 3 | Preset names and 540 bank values | Global controls 0–2 and the trailer are outside these checks. |
| FabFilter Pro-MB | 21 | Entire bank and selected preset | Exact saved bytes |
| FabFilter Pro-G | 9 | Entire bank and selected preset | Exact saved bytes |
| FabFilter Timeless 2 | 8 | Entire bank and selected preset | Stays on Timeless 2. |
| FabFilter Saturn 1 | 7 | All 899 saved values | Stays on Saturn 1. |
| ValhallaShimmer | 8 | All 12 parameter values and other numeric XML settings | The spelling of decimal values can change. |
| Sugar Bytes Effectrix | 20 | Old banks loaded through the native plugin's importer; saved values and program capacity | Two other banks are excluded. Configured controls and automation are not supported by this route yet. |

**Adding another plugin:** Start with one preset. Change one knob, save it again and compare the two saves. DawDreamer can automate the knob changes and saves, and Ghidra can help figure out the binary formats. Once you know where the settings live, try changing them and loading the result back into the plugin. Test a few real presets and listen to them too. You can then use that map to edit old presets or build a converter for a newer plugin. The [Python guide](../../doc/PLUGIN_STATE_MODELS.md) covers how to put it into abletoolz.
