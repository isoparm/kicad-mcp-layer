# Test fixtures: origin and licences

These are not the library's own work and are not under its MIT licence. They are KiCad-authored projects,
used unchanged except for the upgrade to the KiCad 10.0 file formats by `kicad-cli`, as inputs for the
tests. They come from the demo projects shipped with KiCad 10.0.6 (`share/kicad/demos`, also in KiCad's
source repository under `demos/`), and stay under their own terms:

| fixture | source | licence |
| --- | --- | --- |
| `complex_hierarchy/` | KiCad demo "Complex hierarchy" | KiCad's demo terms (see the KiCad repository, `demos/`) |
| `pic_programmer/` | KiCad demo "JDM - COM84 PIC Programmer" (company: KiCad) | KiCad's demo terms |
| `multichannel/` | KiCad demo "multichannel mixer" | CC BY-SA 4.0, as the files declare |
| `_extra_sheets/channel_strip_table.kicad_sch` | one sheet of the multichannel demo | CC BY-SA 4.0, as the file declares |
| `_extra_sheets/fp_connectors.kicad_sch` | one sheet of a KiCad demo | CC BY-SA 4.0, as the file declares |
| `_extra_sheets/tinytapeout-demo.kicad_sch` | KiCad demo `tiny_tapeout` (Psychogenic Technologies) | Apache 2.0, as the file declares |
| `_extra_sheets/usb_hub.kicad_sch` | one sheet of KiCad demo `jetson-agx-thor-baseboard` (Antmicro) | Apache 2.0, as the file declares |

Rule for this folder (`CLAUDE.md`): fixtures must stay KiCad-authored; never hand-write one. Artefacts the
tests or KiCad create inside them are ignored by `.gitignore`.
